import collections
import csv
import functools
import json
import os
import re
import requests
from collections import OrderedDict
from copy import deepcopy
from urllib.parse import urlparse, urljoin

import json_merge_patch
import jsonref
from cached_property import cached_property
from django.conf import settings
from flattentool.schema import get_property_type_set
from jsonschema import FormatChecker, RefResolver
from jsonschema.exceptions import ValidationError
from jsonschema.validators import Draft4Validator as validator


uniqueItemsValidator = validator.VALIDATORS.pop("uniqueItems")

LANGUAGE_RE = re.compile("^(.*_(((([A-Za-z]{2,3}(-([A-Za-z]{3}(-[A-Za-z]{3}){0,2}))?)|[A-Za-z]{4}|[A-Za-z]{5,8})(-([A-Za-z]{4}))?(-([A-Za-z]{2}|[0-9]{3}))?(-([A-Za-z0-9]{5,8}|[0-9][A-Za-z0-9]{3}))*(-([0-9A-WY-Za-wy-z](-[A-Za-z0-9]{2,8})+))*(-(x(-[A-Za-z0-9]{1,8})+))?)|(x(-[A-Za-z0-9]{1,8})+)))$")

validation_error_lookup = {'date-time': 'Date is not in the correct format',
                           'uri': 'Invalid \'uri\' found',
                           'string': 'Value is not a string',
                           'integer': 'Value is not a integer',
                           'number': 'Value is not a number',
                           'object': 'Value is not an object',
                           'array': 'Value is not an array'}


config = settings.COVE_CONFIG_BY_NAMESPACE
cove_ocds_config = {key: config[key]['cove-ocds'] if 'cove-ocds' in config[key] else config[key]['default']for key in config}
cove_360_config = {key: config[key]['cove-360'] if 'cove-360' in config[key] else config[key]['default']for key in config}


class CustomJsonrefLoader(jsonref.JsonLoader):
    def __init__(self, **kwargs):
        self.schema_url = kwargs.pop('schema_url', None)
        super().__init__(**kwargs)

    def get_remote_json(self, uri, **kwargs):
        # ignore url in ref apart from last part
        uri = self.schema_url + uri.split('/')[-1]
        if uri[:4] == 'http':
            return super().get_remote_json(uri, **kwargs)
        else:
            with open(uri) as schema_file:
                return json.load(schema_file, **kwargs)


class CustomRefResolver(RefResolver):
    def __init__(self, *args, **kw):
        self.schema_file = kw.pop('schema_file', None)
        self.schema_url = kw.pop('schema_url', '')
        super().__init__(*args, **kw)

    def resolve_remote(self, uri):
        schema_name = self.schema_file or uri.split('/')[-1]
        uri = urljoin(self.schema_url, schema_name)
        document = self.store.get(uri)

        if document:
            return document
        if self.schema_url.startswith("http"):
            return super().resolve_remote(uri)
        else:
            with open(uri) as schema_file:
                result = json.load(schema_file)

        add_is_codelist(result)
        self.store[uri] = result
        return result


class SchemaMixin():
    @cached_property
    def release_schema_str(self):
        return requests.get(self.release_schema_url).text

    @cached_property
    def release_pkg_schema_str(self):
        uri_scheme = urlparse(self.release_pkg_schema_url).scheme
        if uri_scheme == 'http' or uri_scheme == 'https':
            return requests.get(self.release_pkg_schema_url).text
        else:
            with open(self.release_pkg_schema_url) as fp:
                return fp.read()

    @property
    def _release_schema_obj(self):
        return json.loads(self.release_schema_str)

    @property
    def _release_pkg_schema_obj(self):
        return json.loads(self.release_pkg_schema_str)

    def deref_schema(self, schema_str):
        return jsonref.loads(schema_str, loader=CustomJsonrefLoader(schema_url=self.schema_host),
                             object_pairs_hook=OrderedDict)

    def get_release_schema_obj(self, deref=False):
        if deref:
            return self.deref_schema(self.release_schema_str)
        return self._release_schema_obj

    def get_release_pkg_schema_obj(self, deref=False):
        if deref:
            return self.deref_schema(self.release_pkg_schema_str)
        return self._release_pkg_schema_obj

    def get_release_pkg_schema_fields(self):
        return set(schema_dict_fields_generator(self.get_release_pkg_schema_obj(deref=True)))


class Schema360(SchemaMixin):
    schema_host = cove_360_config['schema_url']
    release_schema_name = cove_360_config['item_schema_name']
    release_pkg_schema_name = cove_360_config['schema_name']
    release_schema_url = urljoin(schema_host, release_schema_name)
    release_pkg_schema_url = urljoin(schema_host, release_pkg_schema_name)


class SchemaOCDS(SchemaMixin):
    release_schema_name = cove_ocds_config['item_schema_name']
    release_pkg_schema_name = cove_ocds_config['schema_name']['release']
    record_pkg_schema_name = cove_ocds_config['schema_name']['record']
    version_choices = cove_ocds_config['schema_version_choices']
    default_version = cove_ocds_config['schema_version']
    default_schema_host = version_choices[default_version][1]
    default_release_schema_url = urljoin(default_schema_host, release_schema_name)

    def __init__(self, select_version=None, release_data=None):
        '''Build the schema object using an specific OCDS schema version
        
        The version used will be select_version, release_data.get('version') or
        default version, in that order. Invalid version choices in select_version or
        release_data will be skipped and registered as self.invalid_version_argument
        and self.invalid_version_data respectively.
        '''
        self.version = self.default_version
        self.invalid_version_argument = False
        self.invalid_version_data = False
        self.schema_host = self.default_schema_host
        self.extensions = {}
        self.invalid_extension = {}
        self.extended = False
        self.extended_schema_file = None
        self.extended_schema_url = None

        if select_version:
            try:
                self.version_choices[select_version]
            except KeyError:
                select_version = None
                self.invalid_version_argument = True
                print('Not a valid value for `version` argument: using version in the release '
                      'data or the default version if version is missing in the release data')
            else:
                self.version = select_version
                self.schema_host = self.version_choices[select_version][1]

        if release_data:
            data_extensions = release_data.get('extensions', {})
            if data_extensions:
                self.extensions = {ext: tuple() for ext in data_extensions}
            if not select_version:
                release_version = release_data.get('version')
                if release_version:
                    version_choice = self.version_choices.get(release_version)
                    if version_choice:
                        self.version = release_version
                        self.schema_host = version_choice[1]
                    else:
                        self.invalid_version_data = True
        else:
            pass

        self.release_schema_url = urljoin(self.schema_host, self.release_schema_name)
        self.release_pkg_schema_url = urljoin(self.schema_host, self.release_pkg_schema_name)
        self.record_pkg_schema_url = urljoin(self.schema_host, self.record_pkg_schema_name)

    def apply_extensions(self, schema_obj):
        if not self.extensions:
            return
        for extensions_descriptor_url in self.extensions.keys():
            i = extensions_descriptor_url.rfind('/')
            url = '{}/{}'.format(extensions_descriptor_url[:i], 'release-schema.json')

            try:
                extension = requests.get(url)
            except requests.exceptions.RequestException:
                self.invalid_extension[extensions_descriptor_url] = 'fetching failed'
                continue
            if extension.ok:
                try:
                    extension_data = extension.json()
                except json.JSONDecodeError:
                    self.invalid_extension[extensions_descriptor_url] = 'invalid JSON'
                    continue
            else:
                self.invalid_extension[extensions_descriptor_url] = '{}: {}'.format(extension.status_code,
                                                                                    extension.reason.lower())
                continue

            schema_obj = json_merge_patch.merge(schema_obj, extension_data)
            extensions_descriptor = requests.get(extensions_descriptor_url).json()
            self.extensions[extensions_descriptor_url] = (url, extensions_descriptor['name'],
                                                          extensions_descriptor['description'])
            self.extended = True

    def get_release_schema_obj(self, deref=False):
        release_schema_obj = self._release_schema_obj
        if self.extended_schema_file:
            with open(self.extended_schema_file) as fp:
                release_schema_obj = json.load(fp)
        elif self.extensions:
            release_schema_obj = deepcopy(self._release_schema_obj)
            self.apply_extensions(release_schema_obj)
        if deref:
            if self.extended:
                extended_release_schema_str = json.dumps(release_schema_obj)
                release_schema_obj = self.deref_schema(extended_release_schema_str)
            else:
                release_schema_obj = self.deref_schema(self.release_schema_str)
        return release_schema_obj

    def get_release_pkg_schema_obj(self, deref=False):
        package_schema_obj = self._release_pkg_schema_obj
        if deref:
            deref_release_schema_obj = self.get_release_schema_obj(deref=True)
            if self.extended:
                package_schema_obj = deepcopy(self._release_pkg_schema_obj)
                package_schema_obj['properties']['releases']['items'] = {}
                release_pkg_schema_str = json.dumps(package_schema_obj)
                package_schema_obj = self.deref_schema(release_pkg_schema_str)
                package_schema_obj['properties']['releases']['items'].update(deref_release_schema_obj)
            else:
                package_schema_obj = self.deref_schema(self.release_pkg_schema_str)
        return package_schema_obj

    def create_extended_release_schema_file(self, upload_dir, upload_url):
        filepath = os.path.join(upload_dir, 'extended_release_schema.json')
        if not self.extended or os.path.exists(filepath):
            return
        with open(filepath, 'w') as fp:
            release_schema_str = json.dumps(self.get_release_schema_obj(), indent=4)
            fp.write(release_schema_str)
        self.extended_schema_file = filepath
        self.extended_schema_url = urljoin(upload_url, 'extended_release_schema.json')

    @cached_property
    def record_pkg_schema_str(self):
        uri_scheme = urlparse(self.record_pkg_schema_url).scheme
        if uri_scheme == 'http' or uri_scheme == 'https':
            return requests.get(self.record_pkg_schema_url).text
        else:
            with open(self.record_pkg_schema_url) as fp:
                return fp.read()

    @property
    def _record_pkg_schema_obj(self):
        return json.loads(self.record_pkg_schema_str)

    def get_record_pkg_schema_obj(self, deref=False):
        if deref:
            return self.deref_schema(self.record_pkg_schema_str)
        return self._record_pkg_schema_obj

    def get_record_pkg_schema_fields(self):
        return set(schema_dict_fields_generator(self.get_record_pkg_schema_obj(deref=True)))


def unique_ids(validator, ui, instance, schema):
    if ui and validator.is_type(instance, "array"):
        non_unique_ids = set()
        all_ids = set()
        for item in instance:
            try:
                item_id = item.get('id')
            except AttributeError:
                # if item is not a dict
                item_id = None
            if item_id and not isinstance(item_id, list) and not isinstance(item_id, dict):
                if item_id in all_ids:
                    non_unique_ids.add(item_id)
                all_ids.add(item_id)
            else:
                # if there is any item without an id key, or the item is not a dict
                # revert to original validator
                for error in uniqueItemsValidator(validator, ui, instance, schema):
                    yield error
                return

        if non_unique_ids:
            msg = "Non-unique ID Values (first 3 shown):  {}"
            yield ValidationError(msg.format(", ".join(str(x) for x in list(non_unique_ids)[:3])))


def required_draft4(validator, required, instance, schema):
    if not validator.is_type(instance, "object"):
        return
    for property in required:
        if property not in instance:
            yield ValidationError(property)


validator.VALIDATORS.pop("patternProperties")
validator.VALIDATORS["uniqueItems"] = unique_ids
validator.VALIDATORS["required"] = required_draft4


def fields_present_generator(json_data, prefix=''):
    if not isinstance(json_data, dict):
        return
    for key, value in json_data.items():
        if isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    yield from fields_present_generator(item, prefix + '/' + key)
            yield prefix + '/' + key
        elif isinstance(value, dict):
            yield from fields_present_generator(value, prefix + '/' + key)
            yield prefix + '/' + key
        else:
            yield prefix + '/' + key


def get_fields_present(*args, **kwargs):
    counter = collections.Counter()
    counter.update(fields_present_generator(*args, **kwargs))
    return dict(counter)


def schema_dict_fields_generator(schema_dict):
    if 'properties' in schema_dict:
        for property_name, value in schema_dict['properties'].items():
            if 'oneOf' in value:
                property_schema_dicts = value['oneOf']
            else:
                property_schema_dicts = [value]
            for property_schema_dict in property_schema_dicts:
                property_type_set = get_property_type_set(property_schema_dict)
                if 'object' in property_type_set:
                    for field in schema_dict_fields_generator(property_schema_dict):
                        yield '/' + property_name + field
                elif 'array' in property_type_set:
                    fields = schema_dict_fields_generator(property_schema_dict['items'])
                    for field in fields:
                        yield '/' + property_name + field
                yield '/' + property_name


def get_counts_additional_fields(json_data, schema_obj, schema_name, context, fields_regex=False):
    if schema_name == 'record-package-schema.json':
        schema_fields = schema_obj.get_record_pkg_schema_fields()
    else:
        schema_fields = schema_obj.get_release_pkg_schema_fields()

    fields_present = get_fields_present(json_data)
    data_only_all = set(fields_present) - schema_fields
    data_only = set()
    for field in data_only_all:
        parent_field = "/".join(field.split('/')[:-1])
        # only take fields with parent in schema (and top level fields)
        # to make results less verbose
        if not parent_field or parent_field in schema_fields:
            if fields_regex:
                if LANGUAGE_RE.search(field.split('/')[-1]):
                    continue
            data_only.add(field)

    return [('/'.join(key.split('/')[:-1]), key.split('/')[-1], fields_present[key]) for key in data_only]


def get_schema_validation_errors(json_data, schema_obj, schema_name, cell_source_map, heading_source_map, add_checkers=None):
    if schema_name == 'record-package-schema.json':
        pkg_schema_obj = schema_obj.get_record_pkg_schema_obj()
    else:
        pkg_schema_obj = schema_obj.get_release_pkg_schema_obj()

    validation_errors = collections.defaultdict(list)
    format_checker = FormatChecker()
    if add_checkers:
        format_checker.checkers.update(add_checkers)

    if getattr(schema_obj, 'extended', None):
        resolver = CustomRefResolver('', pkg_schema_obj, schema_file=schema_obj.extended_schema_file)
    else:
        resolver = CustomRefResolver('', pkg_schema_obj, schema_url=schema_obj.schema_host)

    our_validator = validator(pkg_schema_obj, format_checker=format_checker, resolver=resolver)
    for n, e in enumerate(our_validator.iter_errors(json_data)):
        message = e.message
        path = "/".join(str(item) for item in e.path)
        path_no_number = "/".join(str(item) for item in e.path if not isinstance(item, int))

        validator_type = e.validator
        if e.validator in ('format', 'type'):
            validator_type = e.validator_value
            if isinstance(e.validator_value, list):
                validator_type = e.validator_value[0]

            new_message = validation_error_lookup.get(validator_type)
            if new_message:
                message = new_message

        value = {"path": path}
        cell_reference = cell_source_map.get(path)

        if cell_reference:
            first_reference = cell_reference[0]
            if len(first_reference) == 4:
                value["sheet"], value["col_alpha"], value["row_number"], value["header"] = first_reference
            if len(first_reference) == 2:
                value["sheet"], value["row_number"] = first_reference

        if not isinstance(e.instance, (dict, list)):
            value["value"] = e.instance

        if e.validator == 'required':
            field_name = e.message
            if len(e.path) > 2:
                if isinstance(e.path[-2], int):
                    parent_name = e.path[-1]
                else:
                    parent_name = e.path[-2]

                field_name = str(parent_name) + ":" + e.message
            heading = heading_source_map.get(path_no_number + '/' + e.message)
            if heading:
                field_name = heading[0][1]
                value['header'] = heading[0][1]
            message = "'{}' is missing but required".format(field_name)
        if e.validator == 'enum':
            if "isCodelist" in e.schema:
                continue
            header = value.get('header')
            if not header:
                header = e.path[-1]
            message = "Invalid code found in '{}'".format(header)

        unique_validator_key = [validator_type, message, path_no_number]
        validation_errors[json.dumps(unique_validator_key)].append(value)
    return dict(validation_errors)


def _get_schema_deprecated_paths(schema_obj, obj=None, current_path=(), deprecated_paths=None):
    '''Get a list of deprecated paths and explanations for deprecation in a schema.

    Deprecated paths are given as tuples of tuples:
    ((path, to, field), (deprecation_version, description))
    '''
    if deprecated_paths is None:
        deprecated_paths = []

    if schema_obj:
        obj = schema_obj.get_release_pkg_schema_obj(deref=True)

    for prop, value in obj.get('properties', {}).items():
        if current_path:
            path = current_path + (prop,)
        else:
            path = (prop,)

        if path not in deprecated_paths:
            if "deprecated" in value:
                deprecated_paths.append((
                    path,
                    (value['deprecated']['deprecatedVersion'], value['deprecated']['description'])
                ))
            elif getattr(value, '__reference__', None) and "deprecated" in value.__reference__:
                deprecated_paths.append((
                    path,
                    (value.__reference__['deprecated']['deprecatedVersion'],
                     value.__reference__['deprecated']['description'])
                ))

        if value.get('type') == 'object':
            _get_schema_deprecated_paths(None, value, path, deprecated_paths)
        elif value.get('type') == 'array' and value['items'].get('properties'):
            _get_schema_deprecated_paths(None, value['items'], path, deprecated_paths)

    return deprecated_paths


def _get_json_data_generic_paths(json_data, path=(), generic_paths=None):
    '''Transform json data into a dictionary with keys made of json paths.

   Key are json paths (as tuples). Values are dictionaries with keys including specific
   indexes (which are not including in the top level keys), eg:

   {'a': 'I am', 'b': ['a', 'list'], 'c': [{'ca': 'ca1'}, {'ca': 'ca2'}, {'cb': 'cb'}]}

   will produce:

   {('a',): {('a',): I am'},
    ('b',): {{(b, 0), 'a'}, {('b', 1): 'list'}},
    ('c', 'ca'): {('c', 0, 'ca'): 'ca1', ('c', 1, 'ca'): 'ca2'},
    ('c', 'cb'): {('c', 1, 'ca'): 'cb'}}
    '''
    if generic_paths is None:
        generic_paths = {}

    if isinstance(json_data, dict):
        iterable = list(json_data.items())
        if not iterable:
            generic_paths[path] = {}
    else:
        iterable = list(enumerate(json_data))
        if not iterable:
            generic_paths[path] = []

    for key, value in iterable:
        if isinstance(value, (dict, list)):
            _get_json_data_generic_paths(value, path + (key,), generic_paths)
        else:
            generic_key = tuple(i for i in path + (key,) if type(i) != int)
            if generic_paths.get(generic_key):
                generic_paths[generic_key][path + (key,)] = value
            else:
                generic_paths[generic_key] = {path + (key,): value}

    return generic_paths


def get_json_data_deprecated_fields(json_data, schema_obj):
    deprecated_schema_paths = _get_schema_deprecated_paths(schema_obj)
    paths_in_data = _get_json_data_generic_paths(json_data)
    deprecated_paths_in_data = [path for path in deprecated_schema_paths if path[0] in paths_in_data]

    # Generate an OrderedDict sorted by deprecated field names (keys) mapping
    # to a unordered tuple of tuples:
    # {deprecated_field: ((path, path... ), (version, description))}
    deprecated_fields = OrderedDict()
    for generic_path in sorted(deprecated_paths_in_data, key=lambda tup: tup[0][-1]):
        deprecated_fields[generic_path[0][-1]] = (
            tuple((key for key in paths_in_data[generic_path[0]].keys())),
            generic_path[1]
        )

    # Order the path tuples in values for deprecated_fields.
    deprecated_fields_output = OrderedDict()
    for field, paths in deprecated_fields.items():
        sorted_paths = tuple(sorted(paths[0]))
        slashed_paths = tuple(("/".join((map(str, sort_path[:-1]))) for sort_path in sorted_paths))
        deprecated_fields_output[field] = {"paths": slashed_paths, "explanation": paths[1]}

    return deprecated_fields_output


def add_is_codelist(obj):
    ''' This is needed so that we can detect enums that are arrays as the jsonschema library does not
    give you any parent information and the codelist property is on the parent in this case. Only applies to
    release.tag in core schema at the moment.'''

    for prop, value in obj.get('properties', {}).items():
        if "codelist" in value:
            if 'array' in value.get('type', ''):
                value['items']['isCodelist'] = True
            else:
                value['isCodelist'] = True

        if value.get('type') == 'object':
            add_is_codelist(value)
        elif value.get('type') == 'array' and value['items'].get('properties'):
            add_is_codelist(value['items'])

    for value in obj.get("definitions", {}).values():
        if "properties" in value:
            add_is_codelist(value)


def _get_schema_codelist_paths(schema_obj, obj=None, current_path=(), codelist_paths=None):
    '''Get a dict of codelist paths including the filename and if they are open.

    codelist paths are given as tuples of tuples:
        {("path", "to", "codelist"): (filename, open?), ..}
    '''
    if codelist_paths is None:
        codelist_paths = {}

    if schema_obj:
        obj = schema_obj.get_release_pkg_schema_obj(deref=True)

    for prop, value in obj.get('properties', {}).items():
        if current_path:
            path = current_path + (prop,)
        else:
            path = (prop,)

        if "codelist" in value and path not in codelist_paths:
            codelist_paths[path] = (value['codelist'], value.get('openCodelist', False))

        if value.get('type') == 'object':
            _get_schema_codelist_paths(None, value, path, codelist_paths)
        elif value.get('type') == 'array' and value['items'].get('properties'):
            _get_schema_codelist_paths(None, value['items'], path, codelist_paths)

    return codelist_paths


@functools.lru_cache()
def _load_codelists(codelist_url, unique_files):
    codelists = {}
    for codelist_file in unique_files:
        try:
            response = requests.get(codelist_url + codelist_file)
            response.raise_for_status()
            reader = csv.DictReader(line.decode("utf8") for line in response.iter_lines())
            codelists[codelist_file] = {}
            for record in reader:
                code = record.get('Code') or record.get('code')
                title = record.get('Title') or record.get('Title_en')
                codelists[codelist_file][code] = title

        except requests.exceptions.RequestException:
            codelists = {}
            break

    return codelists


def _generate_data_path(json_data, path=()):
    for key, value in json_data.items():
        if not value:
            continue
        if isinstance(value, list):
            if isinstance(value[0], dict):
                for num, item in enumerate(value):
                    yield from _generate_data_path(item, path + (key, num))
            else:
                yield path + (key,), value
        elif isinstance(value, dict):
            yield from _generate_data_path(value, path + (key,))
        else:
            yield path + (key,), value


def get_additional_codelist_values(schema_obj, codelist_url, json_data):
    if not codelist_url:
        return {}
    codelist_schema_paths = _get_schema_codelist_paths(schema_obj)
    unique_files = frozenset(value[0] for value in codelist_schema_paths.values())
    codelists = _load_codelists(codelist_url, unique_files)

    additional_codelist_values = {}
    for path, values in _generate_data_path(json_data):
        if not isinstance(values, list):
            values = [values]

        path_no_num = tuple(key for key in path if isinstance(key, str))

        if path_no_num not in codelist_schema_paths:
            continue

        codelist, isopen = codelist_schema_paths[path_no_num]

        codelist_values = codelists.get(codelist)
        if not codelist_values:
            continue

        for value in values:
            if value in codelist_values:
                continue
            if path_no_num not in additional_codelist_values:
                additional_codelist_values[path_no_num] = {
                    "path": "/".join(path_no_num[:-1]),
                    "field": path_no_num[-1],
                    "codelist": codelist,
                    "codelist_url": codelist_url + codelist,
                    "isopen": isopen,
                    "values": set(),
                    #"location_values": []
                }

            additional_codelist_values[path_no_num]['values'].add(value)
            #additional_codelist_values[path_no_num]['location_values'].append((path, value))

    return additional_codelist_values
