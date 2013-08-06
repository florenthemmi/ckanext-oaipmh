OAI-PMH Plugin for CKAN
=======================
This plugin provides two things: a harvester which can be configured to harvest
datasets from a OAI-PMH data source and a fully compatible interface for OAI-PMH
which can list all datasets and resources in CKAN for OAI-PMH.

Installation
---------

This plugin requires Pyoai. Pyoai have dependencies so you will need to install this dependencies with:

  sudo apt-get install -y libxml2-dev libxslt1-dev

Then, you can do:

  . /usr/lib/ckan/default/bin/activate

  pip install -e git+https://github.com/opendatasoft/ckanext-oaipmh#egg=ckanext-oaipm

To make OAI-PMH harvester working in CKAN, add the extension name 'oaipmh_harvester'
to the configuration option 'ckan.plugins' of the CKAN ini file in use.

In any case, you should already have 'harvest ckan_harvester' in the configuration option.

Now restart CKAN.

Harvester
---------

Then navigate to http://localhost:5000/harvest to see your existing harvest sources.
Navigate to http://localhost:5000/harvest/new to add a new harvesting source.
For this source do:

* Fill in URL to a OAI-PMH repository.
* Select 'Source Type' to be 'OAI-PMH'.
* In configuration, you must add your selected sets that should be imported.
* Click save

The OAI-PMH harvester support a number of configuration options to control their behaviour. Those need to be defined as a JSON object in the configuration form field. The currently supported configuration options are:

* **default_tags**: A list of tags that will be added to all harvested datasets. Tags don't need to previously exist.
* **default_extras**: A dictionary of key value pairs that will be added to extras of the harvested datasets (existing extras are overwritten).
* **force_all**: By default, after the first harvesting, the harvester will gather only the modified packages from the remote site since the last harvesting. Setting this property to true will force the harvester to gather all remote packages regardless of the modification date. Default is False.

Here is an example of a configuration object (the one that must be entered in the configuration field):

::

  {
    "default_tags": ["ods"],
    "default_extras": {"company": "OpenDataSoft"},
    "force_all": true
  }

You may need to configure your fetch and gather consumer to be run as daemons or
via a the paster commands.

This is clearly documented in ckanext-harvest extension, see it here:

 https://github.com/okfn/ckanext-harvest/blob/master/README.rst

Please note that this fork work with the latest version of `CKAN <https://github.com/okfn/ckan>`_ (tested with v2.2a) and `CKAN harvester <https://github.com/okfn/ckanext-harvest>`_.

As documented in the code, resource type for a dataset is now automatically detected.
The following formats are supported by CKAN and are implemented in the OAI-PMH harvester: "rdf", "pdf", "api", "zip", "xls", "csv", "txt", "xml", "json" and "html".
In CKAN harvester, all unknown resource type use the "data" format for displaying purposes.

This plugin use "html" for the default format of a resource (if not found).
To be recognized, the format need to be at the end of the resource. For example:

* http://my.data.com/my-generated-resource?csv
* http://my.data.com/my-generated-resource?format=csv
* http://my.data.com/my-resource.csv

Interface
---------

The interface is simple to install, add the extension name 'oaipmh' to the
configuration option 'ckan.plugins' of the CKAN ini file in use.

To acccess the interface, go to http://localhost:5000/oai. Use the interface as
described in OAI-PMH documentation.

Tests
-----

This extension offers a suite of tests, to run them, issue the following
command:
  python setup.py nosetests

If you get an error about test.ini not being found, please modify the test-core.ini
file to have:

  use = config:../pyenv/src/ckan/test.ini

pointing to a CKAN source tree
