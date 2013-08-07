"""
Harvester for OAI-PMH interfaces.
"""
#pylint: disable-msg=E1101,E0611,F0401
import logging
import json
import urllib2
import urllib
import sys
import httplib

from lxml import etree
from dataconverter import oai_dc2ckan

import datetime
from ckan.model import Session, Package, Group, Member
from ckan import model
from ckanext.harvest.harvesters.base import HarvesterBase
from ckanext.harvest.model import HarvestObject, HarvestJob
from ckan.model.authz import setup_default_user_roles
from ckan.lib import helpers as h
from pylons import config
import oaipmh.client
from oaipmh.metadata import MetadataReader, MetadataRegistry, oai_dc_reader
from oaipmh.error import NoSetHierarchyError, NoRecordsMatchError
from oaipmh.error import XMLSyntaxError
from oaipmh import common


log = logging.getLogger(__name__)

import socket
socket.setdefaulttimeout(30)

import traceback


class KataMetadataReader(MetadataReader):
    def __call__(self, element):
        map_ = {}
        # create XPathEvaluator for this element
        xpath_evaluator = etree.XPathEvaluator(element, 
                                               namespaces=self._namespaces)
        
        e = xpath_evaluator.evaluate
        # now extra field info according to xpath expr
        for field_name, (field_type, expr) in self._fields.items():
            if field_type == 'bytes':
                value = str(e(expr))
            elif field_type == 'bytesList':
                value = [str(item) for item in e(expr)]
            elif field_type == 'text':
                # make sure we get back unicode strings instead
                # of lxml.etree._ElementUnicodeResult objects.
                value = unicode(e(expr))
            elif field_type == 'textList':
                # make sure we get back unicode strings instead
                # of lxml.etree._ElementUnicodeResult objects.
                value = [unicode(v) for v in e(expr)]
            elif field_type == 'node':
                # Structured data. Don't count on knowing what it is but handle
                # it in code elsewhere. Apparently always a list of 1 node.
                value = e(expr)
            else:
                raise ValueError("Unknown field type: %s" % field_type)
            map_[field_name] = value
        return common.Metadata(map_)


# Below namespaces needs to have all namespaces in docs or some things will not
# be found at all.
kata_oai_dc_reader = KataMetadataReader(
    fields={
        'titleNode':       ('node', 'oai_dc:dc/dc:title'),
        'creator':         ('textList', 'oai_dc:dc/dc:creator/text()'),
        'subject':         ('textList', 'oai_dc:dc/dc:subject/text()'),
        'description':     ('textList', 'oai_dc:dc/dc:description/text()'),
        'date':            ('textList', 'oai_dc:dc/dc:date/text()'),
        'type':            ('textList', 'oai_dc:dc/dc:type/text()'),
        'format':          ('textList', 'oai_dc:dc/dc:format/text()'),
        'identifier':      ('textList', 'oai_dc:dc/dc:identifier/text()'),
        'source':          ('textList', 'oai_dc:dc/dc:source/text()'),
        'language':        ('textList', 'oai_dc:dc/dc:language/text()'),
        'relation':        ('textList', 'oai_dc:dc/dc:relation/text()'),
        'coverage':        ('textList', 'oai_dc:dc/dc:coverage/text()'),
        'rightsNode':      ('node', 'oai_dc:dc/dc:rights'),
        'publisherNode':   ('node', 'oai_dc:dc/dc:publisher'),
        'contributorNode': ('node', 'oai_dc:dc/dc:contributor'),
        'formatNode':      ('node', 'oai_dc:dc/dc:hasFormat'),
    },
    namespaces={
        'oai_dc': 'http://www.openarchives.org/OAI/2.0/oai_dc/',
        'dc':     'http://purl.org/dc/elements/1.1/',
        'foaf':   'http://xmlns.com/foaf/0.1/',
        'rdfs':   'http://www.w3.org/2000/01/rdf-schema#',
        'fp':     'http://downlode.org/Code/RDF/File_Properties/schema#',
        'wn':     'http://xmlns.com/wordnet/1.6/'
    }
)


class OAIPMHHarvester(HarvesterBase):
    """
    OAI-PMH Harvester for ckanext-harvester.
    """

    config = None

    metadata_prefix_key = 'metadataPrefix'
    metadata_prefix_value = 'oai_dc'

    def _set_config(self, config_str):
        """
        Set the configuration string.
        """
        if config_str:
            self.config = json.loads(config_str)
        else:
            self.config = {}

    def info(self):
        """
        Return information about this harvester.
        """
        return {
            'name': 'OAI-PMH',
            'title': 'OAI-PMH',
            'description': 'A server which has a OAI-PMH interface available.'
        }

    def validate_config(self, config):
        if not config:
            return config

        try:
            config_obj = json.loads(config)
            allowed_params = ['default_extras', 'default_tags', 'force_all']

            for key in config_obj:
                if key not in allowed_params:
                    raise ValueError('Unknown parameter "%s"' % key)

            if 'default_extras' in config_obj:
                if not isinstance(config_obj['default_extras'], dict):
                    raise ValueError('default_extras must be a dictionary')

            if 'default_tags' in config_obj:
                if not isinstance(config_obj['default_tags'], list):
                    raise ValueError('default_tags must be a list')

            if 'force_all' in config_obj:
                if not isinstance(config_obj['force_all'], bool):
                    raise ValueError('force_all must be boolean')

        except ValueError, e:
            raise e

        return config

    def _datetime_from_str(self, s):
        # Used to get date from settings file when testing harvesting with
        # (semi-open) date interval.
        if s is None:
            return s
        try:
            t = datetime.datetime.strptime(s, '%Y-%m-%dT%H:%M:%S')
            return t
        except ValueError:
            pass
        try:
            t = datetime.datetime.strptime(s, '%Y-%m-%d')
            return t
        except ValueError:
            log.debug('Bad date for %s' % s)
        return None

    def _str_from_datetime(self, dt):
        return dt.strftime('%Y-%m-%dT%H:%M:%S')

    def _get_client_identifier(self, url, harvest_job=None):
        registry = MetadataRegistry()
        registry.registerReader(self.metadata_prefix_value, kata_oai_dc_reader)
        client = oaipmh.client.Client(url, registry)
        try:
            identifier = client.identify()
        except (urllib2.URLError, urllib2.HTTPError,):
            if harvest_job:
                self._save_gather_error(
                    'Could not gather from %s!' % harvest_job.source.url,
                    harvest_job)
            return client, None
        except socket.error:
            if harvest_job:
                errno, errstr = sys.exc_info()[:2]
                self._save_gather_error(
                    'Socket error OAI-PMH %s, details:\n%s' % (errno, errstr),
                    harvest_job)
            return client, None
        except ValueError:
            # We have no source URL when importing via UI.
            return client, None
        except Exception as e:
            # Guard against miscellaneous stuff. Probably plain bugs.
            log.debug(traceback.format_exc(e))
            return client, None
        return client, identifier

    def _get_group(self, domain, in_revision=True):
        group = Group.by_name(domain)
        if not group:
            if not in_revision:
                model.repo.new_revision()
            group = Group(name=domain, description=domain)
            setup_default_user_roles(group)
            group.save()
            if not in_revision:
                model.repo.commit()
        return group

    def _get_time_limits(self, harvest_job):
        def date_from_config(key):
            return self._datetime_from_str(config.get(key, None))
        from_ = date_from_config('ckanext.harvest.test.from')
        until = date_from_config('ckanext.harvest.test.until')
        previous_job = Session.query(HarvestJob) \
            .filter(HarvestJob.source == harvest_job.source) \
            .filter(HarvestJob.gather_finished != None) \
            .filter(HarvestJob.id != harvest_job.id) \
            .order_by(HarvestJob.gather_finished.desc()).limit(1).first()
        # Settings for debugging override old existing value.
        if previous_job and not from_ and not until:
            from_ = previous_job.gather_started
        from_until = {}
        if from_:
            from_until['from_'] = from_
        if until:
            from_until['until'] = until
        return from_until

    def _gather_stage(self, harvest_job):
        from_until = self._get_time_limits(harvest_job)
        client, identifier = self._get_client_identifier(
            harvest_job.source.url, harvest_job)
        if not identifier:
            raise RuntimeError('Could not get source identifier.')
        # Get things to retry.
        ident2rec, ident2set = {}, {}
        rec_idents = []
        domain = identifier.repositoryName()
        try:
            args = {self.metadata_prefix_key: self.metadata_prefix_value}
            if not self.config.get('force_all', False):
                args.update(from_until)
            for ident in client.listIdentifiers(**args):
                if ident.identifier() in ident2rec:
                    continue  # On our retry list already, do not fetch twice.
                rec_idents.append(ident.identifier())
        except NoRecordsMatchError:
            log.debug('No records matched: %s' % domain)
            pass  # Ok. Just nothing to get.
        except Exception as e:
            # Once we know of something specific, handle it separately.
            log.debug(traceback.format_exc(e))
            self._save_gather_error(
                'Could not fetch identifier list.', harvest_job)
            raise RuntimeError('Could not fetch an identifier list.')
        # Gathering the set list here. Member identifiers in fetch.
        sets = []
        try:
            for set_ in client.listSets():
                identifier, name, _ = set_
                # Is set due for retry and it is not missing member insertion?
                # Set either failed in retry of misses packages but not both.
                # Set with failed insertions may have new members.
                if name in ident2set:
                    continue
                sets.append((identifier, name,))
        except NoSetHierarchyError:
            log.debug('No sets: %s' % domain)
        except urllib2.URLError:
            # Possibly timeout.
            self._save_gather_error(
                'Could not fetch a set list.', harvest_job)
            # We got something so perhaps records can gen gotten, hence [].
            raise RuntimeError('Could not fetch set list.')
        # Since network errors can't occur anymore, it's ok to create the
        # harvest objects to return to caller since we are not missing anything
        # crucial.
        harvest_objs, set_objs, insertion_retries = [], [], set()
        for ident in rec_idents:
            info = {'fetch_type': 'record', 'record': ident, 'domain': domain}
            harvest_obj = HarvestObject(job=harvest_job)
            harvest_obj.content = json.dumps(info)
            harvest_obj.save()
            harvest_objs.append(harvest_obj.id)
        log.info('Gathered %i records from %s.' % (len(harvest_objs), domain,))
        # Add sets to retry first.
        harvest_objs.extend(set_objs)
        for set_id, set_name in sets:
            harvest_obj = HarvestObject(job=harvest_job)
            info = {'fetch_type': 'set', 'set': set_id, 'set_name': set_name, 'domain': domain}
            if 'from_' in from_until:
                info['from_'] = self._str_from_datetime(from_until['from_'])
            if 'until' in from_until:
                info['until'] = self._str_from_datetime(from_until['until'])
            harvest_obj.content = json.dumps(info)
            harvest_obj.save()
            harvest_objs.append(harvest_obj.id)
        log.info(
            'Gathered %i records/sets from %s.' % (len(harvest_objs), domain,))
        return harvest_objs

    def gather_stage(self, harvest_job):
        """
        The gather stage will recieve a HarvestJob object and will be
        responsible for:
            - gathering all the necessary objects to fetch on a later.
              stage (e.g. for a CSW server, perform a GetRecords request)
            - creating the necessary HarvestObjects in the database, specifying
              the guid and a reference to its source and job.
            - creating and storing any suitable HarvestGatherErrors that may
              occur.
            - returning a list with all the ids of the created HarvestObjects.

        :param harvest_job: HarvestJob object
        :returns: A list of HarvestObject ids
        """
        self._set_config(harvest_job.source.config)
        model.repo.new_revision()
        result = None
        try:
            result = self._gather_stage(harvest_job)
        except Exception as e:
            log.error(traceback.format_exc(e))
        model.repo.commit()
        return result

    def fetch_stage(self, harvest_object):
        """
        The fetch stage will receive a HarvestObject object and will be
        responsible for:
            - getting the contents of the remote object (e.g. for a CSW server,
              perform a GetRecordById request).
            - saving the content in the provided HarvestObject.
            - creating and storing any suitable HarvestObjectErrors that may
              occur.
            - returning True if everything went as expected, False otherwise.

        :param harvest_object: HarvestObject object
        :returns: True if everything went right, False if errors were found
        """
        # I needed to store things that don't pickle so import has to do all
        # the work. Well, this avoids saving intermediate info in the DB.
        return True

    def import_stage(self, harvest_object):
        """
        The import stage will receive a HarvestObject object and will be
        responsible for:
            - performing any necessary action with the fetched object (e.g
              create a CKAN package).
              Note: if this stage creates or updates a package, a reference
              to the package must be added to the HarvestObject.
              Additionally, the HarvestObject must be flagged as current.
            - creating the HarvestObject - Package relation (if necessary)
            - creating and storing any suitable HarvestObjectErrors that may
              occur.
            - returning True if everything went as expected, False otherwise.

        :param harvest_object: HarvestObject object
        :returns: True if everything went right, False if errors were found
        """
        # Do common tasks and then call different methods depending on what
        # kind of info the harvest object contains.
        self._set_config(harvest_object.job.source.config)
        ident = json.loads(harvest_object.content)
        registry = MetadataRegistry()
        registry.registerReader(self.metadata_prefix_value, kata_oai_dc_reader)
        client = oaipmh.client.Client(harvest_object.job.source.url, registry)
        domain = ident['domain']
        group = Group.get(domain)  # Checked in gather_stage so exists.
        try:
            if ident['fetch_type'] == 'record':
                return self._fetch_import_record(
                    harvest_object, ident, client, group)
            if ident['fetch_type'] == 'set':
                return self._fetch_import_set(
                    harvest_object, ident, client, group)
            # This should not happen...
            log.error('Unknown fetch type: %s' % ident['fetch_type'])
        except Exception as e:
            # Guard against miscellaneous stuff. Probably plain bugs.
            # Also very rare exceptions we haven't seen yet.
            log.debug(traceback.format_exc(e))
        return False

    def _package_name_from_identifier(self, identifier):
        return urllib.quote_plus(urllib.quote_plus(identifier))

    def _metadata(self, metadata):
        default_extras = self.config.get('default_extras', {})
        default_tags = self.config.get('default_tags', [])

        if default_extras:
            for key, value in default_extras.iteritems():
                if not key in metadata:
                    metadata[key] = value

        if 'subject' in metadata and default_tags:
            metadata['subject'].extend([t for t in default_tags if t not in metadata['subject']])

        return metadata

    def _fetch_import_record(self, harvest_object, master_data, client, group):
        # The fetch part.
        try:
            header, metadata, _ = client.getRecord(
                metadataPrefix=self.metadata_prefix_value,
                identifier=master_data['record'])
        except XMLSyntaxError:
            log.error('oai_dc XML syntax error: %s' % master_data['record'])
            self._save_object_error(
                'Syntax error.',
                harvest_object, stage='Fetch')
            return False
        except socket.error:
            errno, errstr = sys.exc_info()[:2]
            self._save_object_error(
                'Socket error OAI-PMH %s, details:\n%s' % (errno, errstr),
                harvest_object, stage='Fetch')
            return False
        except urllib2.URLError:
            self._save_object_error(
                'Failed to fetch record.',
                harvest_object, stage='Fetch')
            return False
        except httplib.BadStatusLine:
            self._save_object_error(
                'Bad HTTP response status line.',
                harvest_object, stage='Fetch')
            return False
        if not metadata:
            # Assume that there is no metadata and not an error.
            # Should this be a cause for retry?
            log.warning('No metadata: %s' % master_data['record'])
            return False
        if 'date' not in metadata.getMap() or not metadata.getMap()['date']:
            self._save_object_error(
                'Missing date: %s' % master_data['record'],
                harvest_object, stage='Fetch')
            return False
        master_data['record'] = (header.identifier(), metadata.getMap())
        # Do not save to database (because we can't json nor pickle _Element).
        # The import stage.
        # Gather all relevant information into a dictionary.
        data = {
            'identifier': master_data['record'][0],
            'metadata': self._metadata(master_data['record'][1])
        }
        data['package_name'] = self._package_name_from_identifier(
            data['identifier'])
        data['package_url'] = '%s?verb=GetRecord&identifier=%s&%s=%s' % (
            harvest_object.job.source.url, data['identifier'],
            self.metadata_prefix_key, self.metadata_prefix_value,)
        return oai_dc2ckan(data, kata_oai_dc_reader._namespaces, group, harvest_object)

    def _fetch_import_set(self, harvest_object, master_data, client, group):
        # Could be genuine fetch or retry of set insertions.
        if 'set' in master_data:
            # Fetch stage.
            args = {self.metadata_prefix_key: self.metadata_prefix_value, 'set': master_data['set']}
            if 'from_' in master_data:
                args['from_'] = self._datetime_from_str(master_data['from_'])
            if 'until' in master_data:
                args['until'] = self._datetime_from_str(master_data['until'])
            ids = []
            try:
                for identity in client.listIdentifiers(**args):
                    ids.append(identity.identifier())
            except NoRecordsMatchError:
                return False  # Ok, empty set. Nothing to do.
            except socket.error:
                errno, errstr = sys.exc_info()[:2]
                self._save_object_error(
                    'Socket error OAI-PMH %s, details:\n%s' % (errno, errstr,),
                    harvest_object, stage='Fetch')
                return False
            except httplib.BadStatusLine:
                self._save_object_error(
                    'Bad HTTP response status line.',
                    harvest_object, stage='Fetch')
                return False
            master_data['record_ids'] = ids
        else:
            log.debug('Reinsert: %s %i' % (master_data['set_name'], len(master_data['record_ids']),))
        # Do not save to DB because we can't.
        # Import stage.
        model.repo.new_revision()
        subg_name = '%s - %s' % (group.name, master_data['set_name'],)
        subgroup = Group.by_name(subg_name)
        if not subgroup:
            subgroup = Group(name=subg_name, description=subg_name)
            setup_default_user_roles(subgroup)
            subgroup.save()
        missed = []
        for ident in master_data['record_ids']:
            pkg_name = self._package_name_from_identifier(ident)
            # Package may have been omitted due to missing metadata.
            pkg = Package.get(pkg_name)
            if pkg:
                subgroup.add_package_by_name(pkg_name)
                subgroup.save()
                if 'set' not in master_data:
                    log.debug('Inserted %s into %s' % (pkg_name, subg_name,))
            else:
                # Either omitted due to missing metadata or fetch error.
                # In the latter case, we want to add record later once the
                # fetch succeeds after retry.
                missed.append(ident)
                if 'set' not in master_data:
                    log.debug('Omitted %s from %s' % (pkg_name, subg_name,))
        if len(missed):
            # Store missing names for retry.
            master_data['record_ids'] = missed
            if 'set' in master_data:
                del master_data['set']  # Omit fetch later.
            harvest_object.content = json.dumps(master_data)
            log.debug('Missed %s %i' % (master_data['set_name'], len(missed),))
        else:
            harvest_object.content = None  # Clear data.
        model.repo.commit()
        return True
