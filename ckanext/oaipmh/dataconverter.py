"""
Contains code to convert metadata dictionary into form that's stored in CKAN
database. Harvester would get a record in import_stage and pass it to this
for storing the actual data in the database.

In retrospect, there should be a mapping of XML paths to package and extra
fields/keys which should handle the parsing. This code will become unwieldy as
more sources with minor variations are added. Repeatability should be known.
"""

import logging
import traceback
import datetime

from ckan import model
from ckan.model import Package, Group
from ckan.model.authz import setup_default_user_roles
from ckan.model.license import LicenseRegister, LicenseOtherPublicDomain
from ckan.model.license import LicenseOtherClosed, LicenseNotSpecified
from ckan.controllers.storage import BUCKET, get_ofs

log = logging.getLogger(__name__)


def oai_dc2ckan(data, namespaces, group=None, harvest_object=None):
    try:
        return _oai_dc2ckan(data, namespaces, group, harvest_object)
    except Exception as e:
        log.debug(traceback.format_exc(e))
    return False


# Annoyingly, attribute such as rdf:about is presented with key such as
# {http://www.w3.org/1999/02/22-rdf-syntax-ns#}about so we have to check the
# end of the key. 
def _find_attribute(node, key_end):
    for key in node.keys():
        loc = key.find(key_end)
        if loc == len(key) - len(key_end):
            return node.get(key)
    return None


# Given information about the license, try to match it with some known one.
def _match_license(text):
    lr = LicenseRegister()
    for lic in lr.licenses:
        if text in (lic.url, lic.id, lic.title,):
            return lic.id
    return None


def _handle_rights(nodes, namespaces):
    d = {}
    if len(nodes):
        decls = nodes[0].xpath('./*[local-name() = "RightsDeclaration"]', namespaces=namespaces)
        if len(decls):
            if len(decls) > 1:
                # This is actually repeatable but not handled so thus far.
                # Package.license field does not allow for multiple values.
                # Convert to loop once multiple licenses are handled.
                log.warning('Multiple RightsDeclarations in one record.')
            category = decls[0].get('RIGHTSCATEGORY')
            text = decls[0].text
        else:  # Probably just old-fashioned text.
            text = nodes[0].text
            category = 'LICENSED'  # Let's give recognizing the license a try.
        if category == 'LICENSED' and text:
            lic = _match_license(text)
            if lic is not None:
                d['package.license'] = {'id': lic}
            else:
                # Something unknown. Store text or license.
                if text.startswith('http://') or text.startswith('https://'):
                    d['licenseURL'] = text
                else:
                    d['licenseText'] = text
        elif category == 'PUBLIC DOMAIN':
            lic = LicenseOtherPublicDomain()
            d['package.license'] = {'id': lic.id}
        elif category in ('CONTRACTUAL', 'OTHER',):
            lic = LicenseOtherClosed()
            d['package.license'] = {'id': lic.id}
        elif category == 'COPYRIGHTED':
            lic = LicenseNotSpecified()
            d['package.license'] = {'id': lic.id}
    return d


def _handle_contributor(nodes, namespaces):
    d = {}
    proj_idx = 0
    for node in nodes:
        # Add iteration over something else when those show up.
        projs = node.xpath('./foaf:Project', namespaces=namespaces)
        if len(projs):
            for pro in projs:
                name = _find_attribute(pro, 'about')
                if name is None:
                    ns = pro.xpath('./foaf:name', namespaces=namespaces)
                    if len(ns) == 0:
                        continue
                    name = ns[0].text
                d['project_%i' % proj_idx] = name
                proj_idx += 1
    if len(nodes) and len(d) == 0:
        d['contributor'] = nodes[0].text
    return d


def _handle_publisher(nodes, namespaces):
    d = {}
    person_idx = 0
    for node in nodes:
        persons = node.xpath('./foaf:person', namespaces=namespaces)
        for p in persons:
            url = _find_attribute(p, 'about')
            ns = p.xpath('./foaf:mbox', namespaces=namespaces)
            email = _find_attribute(ns[0], 'resource') if len(ns) else None
            ns = p.xpath('./foaf:phone', namespaces=namespaces)
            phone = _find_attribute(ns[0], 'resource') if len(ns) else None
            if url:
                d['contactURL_%i' % person_idx] = url
            if phone and len(phone) > 5:  # Filter out '-' and similar.
                d['phone_%i' % person_idx] = phone
            if email and person_idx == 0:  # Just keep first. The rest later?
                d['package.maintainer_email'] = email
            person_idx += 1
    if len(nodes) and len(d) == 0:
        d['publisher'] = nodes[0].text
    return d


def _handle_format(nodes, namespaces):
    d = []
    for node in nodes:
        # Are there others besides File?
        for f in node.xpath('./fp:File', namespaces=namespaces):
            url = _find_attribute(f, 'about')
            if not url:
                continue
            size = None
            # Should be only one.
            for sz in f.xpath('./fp:size', namespaces=namespaces):
                size = sz.text
            checksum = None
            algorithm = None
            # Can there be repeat? At what level? Should warn of repetition.
            for c in f.xpath('./fp:checksum', namespaces=namespaces):
                for ck in c.xpath('./fp:Checksum', namespaces=namespaces):
                    for a in ck.xpath('./fp:generator/wn:Algorithm', namespaces=namespaces):
                        algorithm = _find_attribute(a, 'about')
                    for v in ck.xpath('./fp:checksumValue', namespaces=namespaces):
                        checksum = v.text
            rd = {'url': url}
            if size is not None:
                rd['size'] = size
            if checksum is not None:
                rd['hash'] = checksum
            if algorithm is not None:
                rd['extras'] = algorithm
            d.append(rd)
    return d


def _oai_dc2ckan(data, namespaces, group, harvest_object):
    model.repo.new_revision()
    identifier = data['identifier']
    metadata = data['metadata']
    # Store title in pkg.title and keep all in extras as well. That way
    # UI will work some way in any case.
    title = metadata.get('title', identifier)[0]
    #title = metadata['title'][0] if len(metadata['title']) else identifier
    name = data['package_name']
    pkg = Package.get(name)
    if not pkg:
        pkg = Package(name=name, title=title, id=identifier)
        pkg.save()
        setup_default_user_roles(pkg)
    else:
        log.debug('Updating: %s' % name)
        # There are old resources which are replaced by new ones if they are
        # relevant anymore so "delete" all existing resources now.
        for r in pkg.resources:
            r.state = 'deleted'
    extras = {}
    idx = 0
    for s in ('subject', 'type',):
        for tag in metadata.get(s, []):
            # Turn each subject or type field into it's own tag.
            tagi = tag.strip()
            if tagi.startswith('http://') or tagi.startswith('https://'):
                extras['tag_source_%i' % idx] = tagi
                idx += 1
                tags = []  # URL tags break links in UI.
            else:
                tags = [tagi]
            for tagi in tags:
                tagi = tagi[:100]  # 100 char limit in DB.
                tag_obj = model.Tag.by_name(tagi)
                if not tag_obj:
                    tag_obj = model.Tag(name=tagi)
                    tag_obj.save()
                pkgtag = model.Session.query(model.PackageTag).filter(
                    model.PackageTag.package_id == pkg.id).filter(
                        model.PackageTag.tag_id == tag_obj.id
                    ).limit(1).first()
                if pkgtag is None:
                    pkgtag = model.PackageTag(tag=tag_obj, package=pkg)
                    pkgtag.save()  # Avoids duplicates if tags have duplicates.
    extras.update(
        _handle_contributor(metadata.get('contributorNode', []), namespaces))
    extras.update(
        _handle_publisher(metadata.get('publisherNode', []), namespaces))
    # This value belongs to elsewhere.
    if 'package.maintainer_email' in extras:
        pkg.maintainer_email = extras['package.maintainer_email']
        del extras['package.maintainer_email']
    extras.update(_handle_rights(metadata.get('rightsNode', []), namespaces))
    if 'package.license' in extras:
        pkg.license = extras['package.license']
        del extras['package.license']
    # Check that we have a language.
    lang = metadata.get('language', [])
    if lang is not None and len(lang) and len(lang[0]) > 1:
        pkg.language = lang[0]
    # The rest.
    # description below goes to pkg.notes. I think it should not added here.
    for key, value in metadata.items():
        if value is None or len(value) == 0 or key in (
            'title',
            'description',
            'publisherNode',
            'contributorNode',
            'formatNode',
            'identifier',
            'source',
            'rightsNode'
        ):
            continue
        extras[key] = value[0]
    #description = metadata['description'][0] if len(metadata['description']) else ''
    notes = ' '.join(metadata.get('description', []))
    pkg.notes = notes.replace('\n', ' ').replace('  ', ' ')
    if 'date' in extras:
        pkg.version = extras['date']
        extras['modified'] = extras['date']
        del extras['date']
    pkg.extras = extras
    pkg.url = data['package_url']
    if 'package_resource' in data:
        try:
            ofs = get_ofs()
            ofs.put_stream(BUCKET, data['package_xml_save']['label'], data['package_xml_save']['xml'], {})
            pkg.add_resource(**(data['package_resource']))
        except KeyError:
            pass
    if harvest_object is not None:
        harvest_object.package_id = pkg.id
        harvest_object.content = None
        harvest_object.current = True
        harvest_object.save()
    # Metadata may have different identifiers, pick link, if exists.

    # See: https://github.com/okfn/ckan/blob/master/ckan/public/base/images/sprite-resource-icons.png
    # "Data" format is used by CKAN to identify unknown resources.
    # You can use it if you want (default format is "html"). For example:
    # - http://my.data.com/my-generated-resource?data
    # - http://my.data.com/my-resource.data
    available_formats = ['data', 'rdf', 'pdf', 'api', 'zip', 'xls', 'csv', 'txt', 'xml', 'json', 'html']
    default_format = 'html'

    for ids in metadata['identifier']:
        if ids.startswith('http://') or ids.startswith('https://'):
            # The end of the URL must be the format, otherwise it will use "html" by default
            infer_format = default_format

            for ext in available_formats:
                if ids.endswith(ext):
                    infer_format = ext

            pkg.add_resource(ids, name=pkg.title, format=infer_format)
    # All belong to the main group even if they do not belong to any set.
    if group is not None:
        group.add_package_by_name(pkg.name)
    model.repo.commit()
    return pkg.id

