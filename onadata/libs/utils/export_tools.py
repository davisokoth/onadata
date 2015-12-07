import csv
from datetime import datetime, date
import json
import hashlib
import os
import re
import six
from urlparse import urlparse
from zipfile import ZipFile

from django.core.files.base import File
from django.core.files.temp import NamedTemporaryFile
from django.core.files.storage import get_storage_class
from django.contrib.auth.models import User
from django.shortcuts import render_to_response
from openpyxl.utils.datetime import to_excel
from openpyxl.workbook import Workbook
from pyxform.question import Question
from pyxform.section import Section, RepeatingSection
from savReaderWriter import SavWriter
from json2xlsclient.client import Client

from onadata.apps.logger.models import Attachment
from onadata.apps.logger.models import Instance
from onadata.apps.logger.models import OsmData
from onadata.apps.logger.models import XForm
from onadata.apps.logger.models.data_view import DataView
from onadata.apps.main.models.meta_data import MetaData
from onadata.apps.viewer.models.export import Export
from onadata.apps.viewer.models.parsed_instance import\
    _is_invalid_for_mongo, _encode_for_mongo, _decode_from_mongo,\
    query_data
from onadata.libs.exceptions import J2XException, NoRecordsFoundError
from onadata.libs.utils.viewer_tools import create_attachments_zipfile,\
    image_urls
from onadata.libs.utils.common_tags import (
    ID, XFORM_ID_STRING, STATUS, ATTACHMENTS, GEOLOCATION, BAMBOO_DATASET_ID,
    DELETEDAT, INDEX, PARENT_INDEX, PARENT_TABLE_NAME, GROUPNAME_REMOVED_FLAG,
    SUBMISSION_TIME, UUID, TAGS, NOTES, VERSION, SUBMITTED_BY, DURATION,
    DATAVIEW_EXPORT)
from onadata.libs.utils.osm import get_combined_osm


QUESTION_TYPES_TO_EXCLUDE = [
    u'note',
]
# the bind type of select multiples that we use to compare
MULTIPLE_SELECT_BIND_TYPE = u"select"
GEOPOINT_BIND_TYPE = u"geopoint"

DEFAULT_GROUP_DELIMITER = '/'
EXPORT_QUERY_KEY = 'query'


def encode_if_str(row, key, encode_dates=False):
    val = row.get(key)

    if isinstance(val, six.string_types):
        return val.encode('utf-8')

    if encode_dates and isinstance(val, datetime):
        try:
            return val.strftime('%Y-%m-%dT%H:%M:%S%z').encode('utf-8')
        except ValueError:
            raise Exception(u"%s has an invalid datetime format" % (val))

    if encode_dates and isinstance(val, date):
        try:
            return val.strftime('%Y-%m-%d').encode('utf-8')
        except ValueError:
            raise Exception(u"%s has an invalid date format" % (val))

    return val


def question_types_to_exclude(_type):
    return _type in QUESTION_TYPES_TO_EXCLUDE


def str_to_bool(s):
    if s in ['True', 'true', 'TRUE']:
        return True
    else:
        return False


class DictOrganizer(object):

    def set_dict_iterator(self, dict_iterator):
        self._dict_iterator = dict_iterator

    # Every section will get its own table
    # I need to think of an easy way to flatten out a dictionary
    # parent name, index, table name, data
    def _build_obs_from_dict(self, d, obs, table_name,
                             parent_table_name, parent_index):
        if table_name not in obs:
            obs[table_name] = []
        this_index = len(obs[table_name])
        obs[table_name].append({
            u"_parent_table_name": parent_table_name,
            u"_parent_index": parent_index,
        })
        for k, v in d.items():
            if type(v) != dict and type(v) != list:
                assert k not in obs[table_name][-1]
                obs[table_name][-1][k] = v
        obs[table_name][-1][u"_index"] = this_index

        for k, v in d.items():
            if type(v) == dict:
                kwargs = {
                    "d": v,
                    "obs": obs,
                    "table_name": k,
                    "parent_table_name": table_name,
                    "parent_index": this_index
                }
                self._build_obs_from_dict(**kwargs)
            if type(v) == list:
                for i, item in enumerate(v):
                    kwargs = {
                        "d": item,
                        "obs": obs,
                        "table_name": k,
                        "parent_table_name": table_name,
                        "parent_index": this_index,
                    }
                    self._build_obs_from_dict(**kwargs)
        return obs

    def get_observation_from_dict(self, d):
        result = {}
        assert len(d.keys()) == 1
        root_name = d.keys()[0]
        kwargs = {
            "d": d[root_name],
            "obs": result,
            "table_name": root_name,
            "parent_table_name": u"",
            "parent_index": -1,
        }
        self._build_obs_from_dict(**kwargs)
        return result


def dict_to_joined_export(data, index, indices, name):
    """
    Converts a dict into one or more tabular datasets
    """
    output = {}
    # TODO: test for _geolocation and attachment lists
    if isinstance(data, dict):
        for key, val in data.iteritems():
            if isinstance(val, list) and key not in [NOTES, TAGS]:
                output[key] = []
                for child in val:
                    if key not in indices:
                        indices[key] = 0
                    indices[key] += 1
                    child_index = indices[key]
                    new_output = dict_to_joined_export(
                        child, child_index, indices, key)
                    d = {INDEX: child_index, PARENT_INDEX: index,
                         PARENT_TABLE_NAME: name}
                    # iterate over keys within new_output and append to
                    # main output
                    for out_key, out_val in new_output.iteritems():
                        if isinstance(out_val, list):
                            if out_key not in output:
                                output[out_key] = []
                            output[out_key].extend(out_val)
                        else:
                            d.update(out_val)
                    output[key].append(d)
            else:
                if name not in output:
                    output[name] = {}
                if key in [TAGS]:
                    output[name][key] = ",".join(val)
                elif key in [NOTES]:
                    note_list = [v if isinstance(v, six.string_types)
                                 else v['note'] for v in val]
                    output[name][key] = "\r\n".join(note_list)
                else:
                    output[name][key] = val

    return output


class ExportBuilder(object):
    IGNORED_COLUMNS = [XFORM_ID_STRING, STATUS, GEOLOCATION,
                       BAMBOO_DATASET_ID, DELETEDAT]
    # fields we export but are not within the form's structure
    EXTRA_FIELDS = [ID, UUID, SUBMISSION_TIME, INDEX, PARENT_TABLE_NAME,
                    PARENT_INDEX, TAGS, NOTES, ATTACHMENTS, VERSION, DURATION,
                    SUBMITTED_BY]
    SPLIT_SELECT_MULTIPLES = True
    BINARY_SELECT_MULTIPLES = False

    # column group delimiters
    GROUP_DELIMITER_SLASH = '/'
    GROUP_DELIMITER_DOT = '.'
    GROUP_DELIMITER = GROUP_DELIMITER_SLASH
    GROUP_DELIMITERS = [GROUP_DELIMITER_SLASH, GROUP_DELIMITER_DOT]

    INCLUDE_LABELS = False
    INCLUDE_LABELS_ONLY = False

    TYPES_TO_CONVERT = ['int', 'decimal', 'date']  # , 'dateTime']
    CONVERT_FUNCS = {
        'int': lambda x: int(x),
        'decimal': lambda x: float(x),
        'date': lambda x: ExportBuilder.string_to_date_with_xls_validation(x),
        'dateTime': lambda x: datetime.strptime(x[:19], '%Y-%m-%dT%H:%M:%S')
    }

    TRUNCATE_GROUP_TITLE = False

    XLS_SHEET_NAME_MAX_CHARS = 31

    @classmethod
    def string_to_date_with_xls_validation(cls, date_str):
        date_obj = datetime.strptime(date_str, '%Y-%m-%d').date()
        try:
            to_excel(date_obj)
        except ValueError:
            return date_str
        else:
            return date_obj

    @classmethod
    def format_field_title(cls, abbreviated_xpath, field_delimiter,
                           data_dictionary, remove_group_name=False):
        title = abbreviated_xpath
        # Check if to truncate the group name prefix
        if remove_group_name:
            elem = data_dictionary.get_survey_element(abbreviated_xpath)
            # incase abbreviated_xpath is a choices xpath
            if elem is None:
                pass
            elif elem.type == u'':
                title = u'/'.join([elem.parent.name, elem.name])
            else:
                title = elem.name

        if field_delimiter != '/':
            title = field_delimiter.join(title.split('/'))

        return title

    def set_survey(self, survey):
        # TODO resolve circular import
        from onadata.apps.viewer.models.data_dictionary import\
            DataDictionary
        dd = DataDictionary()
        dd._survey = survey

        def build_sections(
                current_section, survey_element, sections, select_multiples,
                gps_fields, encoded_fields, field_delimiter='/',
                remove_group_name=False):
            for child in survey_element.children:
                current_section_name = current_section['name']
                # if a section, recurs
                if isinstance(child, Section):
                    # if its repeating, build a new section
                    if isinstance(child, RepeatingSection):
                        # section_name in recursive call changes
                        section = {
                            'name': child.get_abbreviated_xpath(),
                            'elements': []}
                        self.sections.append(section)
                        build_sections(
                            section, child, sections, select_multiples,
                            gps_fields, encoded_fields, field_delimiter,
                            remove_group_name)
                    else:
                        # its a group, recurs using the same section
                        build_sections(
                            current_section, child, sections, select_multiples,
                            gps_fields, encoded_fields, field_delimiter,
                            remove_group_name)
                elif isinstance(child, Question) and child.bind.get(u"type")\
                        not in QUESTION_TYPES_TO_EXCLUDE:
                    # add to survey_sections
                    if isinstance(child, Question):
                        child_xpath = child.get_abbreviated_xpath()
                        _title = ExportBuilder.format_field_title(
                            child.get_abbreviated_xpath(),
                            field_delimiter, dd, remove_group_name
                        )
                        _label = \
                            dd.get_label(child_xpath, elem=child) or _title
                        current_section['elements'].append({
                            'label': _label,
                            'title': _title,
                            'xpath': child_xpath,
                            'type': child.bind.get(u"type")
                        })

                        if _is_invalid_for_mongo(child_xpath):
                            if current_section_name not in encoded_fields:
                                encoded_fields[current_section_name] = {}
                            encoded_fields[current_section_name].update(
                                {child_xpath: _encode_for_mongo(child_xpath)})

                    # if its a select multiple, make columns out of its choices
                    if child.bind.get(u"type") == MULTIPLE_SELECT_BIND_TYPE\
                            and self.SPLIT_SELECT_MULTIPLES:
                        for c in child.children:
                            _xpath = c.get_abbreviated_xpath()
                            _title = ExportBuilder.format_field_title(
                                _xpath, field_delimiter, dd, remove_group_name)
                            _label = dd.get_label(_xpath, elem=c) or _title
                            choice = {
                                'label': field_delimiter.join([
                                    child.name, _label
                                ]),
                                'title': _title,
                                'xpath': _xpath,
                                'type': 'string'
                            }

                            if choice not in current_section['elements']:
                                current_section['elements'].append(choice)
                        _append_xpaths_to_section(
                            current_section_name, select_multiples,
                            child.get_abbreviated_xpath(),
                            [c.get_abbreviated_xpath()
                             for c in child.children])

                    # split gps fields within this section
                    if child.bind.get(u"type") == GEOPOINT_BIND_TYPE:
                        # add columns for geopoint components
                        xpaths = DataDictionary.get_additional_geopoint_xpaths(
                            child.get_abbreviated_xpath())
                        for xpath in xpaths:
                            _title = ExportBuilder.format_field_title(
                                xpath, field_delimiter, dd,
                                remove_group_name
                            )
                            current_section['elements'].append({
                                'label': _title,
                                'title': _title,
                                'xpath': xpath,
                                'type': 'decimal'
                            })
                        _append_xpaths_to_section(
                            current_section_name, gps_fields,
                            child.get_abbreviated_xpath(), xpaths)

        def _append_xpaths_to_section(current_section_name, field_list, xpath,
                                      xpaths):
            if current_section_name not in field_list:
                field_list[current_section_name] = {}
            field_list[
                current_section_name][xpath] = xpaths

        self.survey = survey
        self.select_multiples = {}
        self.gps_fields = {}
        self.encoded_fields = {}
        main_section = {'name': survey.name, 'elements': []}
        self.sections = [main_section]
        build_sections(
            main_section, self.survey, self.sections,
            self.select_multiples, self.gps_fields, self.encoded_fields,
            self.GROUP_DELIMITER, self.TRUNCATE_GROUP_TITLE)

    def section_by_name(self, name):
        matches = filter(lambda s: s['name'] == name, self.sections)
        assert(len(matches) == 1)
        return matches[0]

    @classmethod
    def split_select_multiples(cls, row, select_multiples):
        # for each select_multiple, get the associated data and split it
        for xpath, choices in select_multiples.iteritems():
            # get the data matching this xpath
            data = row.get(xpath)
            selections = []
            if data:
                selections = [
                    u'{0}/{1}'.format(
                        xpath, selection) for selection in data.split()]
            if not cls.BINARY_SELECT_MULTIPLES:
                row.update(dict(
                    [(choice, choice in selections if selections else None)
                     for choice in choices]))
            else:
                YES = 1
                NO = 0
                row.update(dict(
                    [(choice, YES if choice in selections else NO)
                     for choice in choices]))
        return row

    @classmethod
    def split_gps_components(cls, row, gps_fields):
        # for each gps_field, get associated data and split it
        for xpath, gps_components in gps_fields.iteritems():
            data = row.get(xpath)
            if data:
                gps_parts = data.split()
                if len(gps_parts) > 0:
                    row.update(zip(gps_components, gps_parts))
        return row

    @classmethod
    def decode_mongo_encoded_fields(cls, row, encoded_fields):
        for xpath, encoded_xpath in encoded_fields.iteritems():
            if row.get(encoded_xpath):
                val = row.pop(encoded_xpath)
                row.update({xpath: val})
        return row

    @classmethod
    def decode_mongo_encoded_section_names(cls, data):
        return dict([(_decode_from_mongo(k), v) for k, v in data.iteritems()])

    @classmethod
    def convert_type(cls, value, data_type):
        """
        Convert data to its native type e.g. string '1' to int 1
        @param value: the string value to convert
        @param data_type: the native data type to convert to
        @return: the converted value
        """
        func = ExportBuilder.CONVERT_FUNCS.get(data_type, lambda x: x)
        try:
            return func(value)
        except ValueError:
            return value

    def pre_process_row(self, row, section):
        """
        Split select multiples, gps and decode . and $
        """
        section_name = section['name']

        # first decode fields so that subsequent lookups
        # have decoded field names
        if section_name in self.encoded_fields:
            row = ExportBuilder.decode_mongo_encoded_fields(
                row, self.encoded_fields[section_name])

        if self.SPLIT_SELECT_MULTIPLES and\
                section_name in self.select_multiples:
            row = ExportBuilder.split_select_multiples(
                row, self.select_multiples[section_name])

        if section_name in self.gps_fields:
            row = ExportBuilder.split_gps_components(
                row, self.gps_fields[section_name])

        # convert to native types
        for elm in section['elements']:
            # only convert if its in our list and its not empty, just to
            # optimize
            value = row.get(elm['xpath'])
            if elm['type'] in ExportBuilder.TYPES_TO_CONVERT\
                    and value is not None and value != '':
                row[elm['xpath']] = ExportBuilder.convert_type(
                    value, elm['type'])

        return row

    def to_zipped_csv(self, path, data, *args, **kwargs):
        def write_row(row, csv_writer, fields):
            csv_writer.writerow(
                [encode_if_str(row, field) for field in fields])

        csv_defs = {}
        dataview = kwargs.get('dataview')

        for section in self.sections:
            csv_file = NamedTemporaryFile(suffix=".csv")
            csv_writer = csv.writer(csv_file)
            csv_defs[section['name']] = {
                'csv_file': csv_file, 'csv_writer': csv_writer}

        # write headers
        if not self.INCLUDE_LABELS_ONLY:
            for section in self.sections:
                fields = self.get_fields(dataview, section, 'title')
                csv_defs[section['name']]['csv_writer'].writerow(
                    [f.encode('utf-8') for f in fields])

        # write labels
        if self.INCLUDE_LABELS or self.INCLUDE_LABELS_ONLY:
            for section in self.sections:
                fields = self.get_fields(dataview, section, 'label')
                csv_defs[section['name']]['csv_writer'].writerow(
                    [f.encode('utf-8') for f in fields])

        index = 1
        indices = {}
        survey_name = self.survey.name
        for d in data:
            # decode mongo section names
            joined_export = dict_to_joined_export(d, index, indices,
                                                  survey_name)
            output = ExportBuilder.decode_mongo_encoded_section_names(
                joined_export)
            # attach meta fields (index, parent_index, parent_table)
            # output has keys for every section
            if survey_name not in output:
                output[survey_name] = {}
            output[survey_name][INDEX] = index
            output[survey_name][PARENT_INDEX] = -1
            for section in self.sections:
                # get data for this section and write to csv
                section_name = section['name']
                csv_def = csv_defs[section_name]
                fields = self.get_fields(dataview, section, 'xpath')
                csv_writer = csv_def['csv_writer']
                # section name might not exist within the output, e.g. data was
                # not provided for said repeat - write test to check this
                row = output.get(section_name, None)
                if type(row) == dict:
                    write_row(
                        self.pre_process_row(row, section),
                        csv_writer, fields)
                elif type(row) == list:
                    for child_row in row:
                        write_row(
                            self.pre_process_row(child_row, section),
                            csv_writer, fields)
            index += 1

        # write zipfile
        with ZipFile(path, 'w') as zip_file:
            for section_name, csv_def in csv_defs.iteritems():
                csv_file = csv_def['csv_file']
                csv_file.seek(0)
                zip_file.write(
                    csv_file.name, "_".join(section_name.split("/")) + ".csv")

        # close files when we are done
        for section_name, csv_def in csv_defs.iteritems():
            csv_def['csv_file'].close()

    @classmethod
    def get_valid_sheet_name(cls, desired_name, existing_names):
        # a sheet name has to be <= 31 characters and not a duplicate of an
        # existing sheet
        # truncate sheet_name to XLSDataFrameBuilder.SHEET_NAME_MAX_CHARS
        new_sheet_name = \
            desired_name[:cls.XLS_SHEET_NAME_MAX_CHARS]

        # make sure its unique within the list
        i = 1
        generated_name = new_sheet_name
        while generated_name in existing_names:
            digit_length = len(str(i))
            allowed_name_len = cls.XLS_SHEET_NAME_MAX_CHARS - \
                digit_length
            # make name the required len
            if len(generated_name) > allowed_name_len:
                generated_name = generated_name[:allowed_name_len]
            generated_name = "{0}{1}".format(generated_name, i)
            i += 1
        return generated_name

    def to_xls_export(self, path, data, *args, **kwargs):
        def write_row(data, work_sheet, fields, work_sheet_titles):
            # update parent_table with the generated sheet's title
            data[PARENT_TABLE_NAME] = work_sheet_titles.get(
                data.get(PARENT_TABLE_NAME))
            work_sheet.append([data.get(f) for f in fields])

        dataview = kwargs.get('dataview')
        wb = Workbook(optimized_write=True)
        work_sheets = {}
        # map of section_names to generated_names
        work_sheet_titles = {}
        for section in self.sections:
            section_name = section['name']
            work_sheet_title = ExportBuilder.get_valid_sheet_name(
                "_".join(section_name.split("/")), work_sheet_titles.values())
            work_sheet_titles[section_name] = work_sheet_title
            work_sheets[section_name] = wb.create_sheet(
                title=work_sheet_title)

        # write the headers
        if not self.INCLUDE_LABELS_ONLY:
            for section in self.sections:
                section_name = section['name']
                headers = self.get_fields(dataview, section, 'title')

                # get the worksheet
                ws = work_sheets[section_name]
                ws.append(headers)

        # write labels
        if self.INCLUDE_LABELS or self.INCLUDE_LABELS_ONLY:
            for section in self.sections:
                section_name = section['name']
                labels = self.get_fields(dataview, section, 'label')

                # get the worksheet
                ws = work_sheets[section_name]
                ws.append(labels)

        index = 1
        indices = {}
        survey_name = self.survey.name
        for d in data:
            joined_export = dict_to_joined_export(d, index, indices,
                                                  survey_name)
            output = ExportBuilder.decode_mongo_encoded_section_names(
                joined_export)
            # attach meta fields (index, parent_index, parent_table)
            # output has keys for every section
            if survey_name not in output:
                output[survey_name] = {}
            output[survey_name][INDEX] = index
            output[survey_name][PARENT_INDEX] = -1
            for section in self.sections:
                # get data for this section and write to xls
                section_name = section['name']
                fields = self.get_fields(dataview, section, 'xpath')

                ws = work_sheets[section_name]
                # section might not exist within the output, e.g. data was
                # not provided for said repeat - write test to check this
                row = output.get(section_name, None)
                if type(row) == dict:
                    write_row(
                        self.pre_process_row(row, section),
                        ws, fields, work_sheet_titles)
                elif type(row) == list:
                    for child_row in row:
                        write_row(
                            self.pre_process_row(child_row, section),
                            ws, fields, work_sheet_titles)
            index += 1

        wb.save(filename=path)

    def to_flat_csv_export(
            self, path, data, username, id_string, filter_query,
            start=None, end=None, dataview=None, xform=None):
        # TODO resolve circular import
        from onadata.libs.utils.csv_builder import CSVDataFrameBuilder

        csv_builder = CSVDataFrameBuilder(
            username, id_string, filter_query, self.GROUP_DELIMITER,
            self.SPLIT_SELECT_MULTIPLES, self.BINARY_SELECT_MULTIPLES,
            start, end, self.TRUNCATE_GROUP_TITLE, xform,
            self.INCLUDE_LABELS, self.INCLUDE_LABELS_ONLY
        )
        csv_builder.export_to(path, dataview=dataview)

    def to_zipped_sav(self, path, data, *args, **kwargs):
        def write_row(row, csv_writer, fields):
            sav_writer.writerow(
                [encode_if_str(row, field, True) for field in fields])

        sav_defs = {}

        # write headers
        for section in self.sections:
            _title = 'label' \
                if self.INCLUDE_LABELS or self.INCLUDE_LABELS_ONLY else 'title'
            fields = [element[_title] for element in section['elements']]\
                + self.EXTRA_FIELDS
            c = 0
            var_labels = {}
            var_names = []
            tmp_k = {}
            for field in fields:
                c += 1
                var_name = 'var%d' % c
                var_labels[var_name] = field
                var_names.append(var_name)
                tmp_k[field] = var_name

            var_types = dict(
                [(tmp_k[element[_title]],
                  0 if element['type'] in ['decimal', 'int'] else 255)
                 for element in section['elements']] +
                [(tmp_k[item],
                    0 if item in ['_id', '_index', '_parent_index'] else 255)
                 for item in self.EXTRA_FIELDS]
            )
            sav_file = NamedTemporaryFile(suffix=".sav")
            sav_writer = SavWriter(sav_file.name, varNames=var_names,
                                   varTypes=var_types,
                                   varLabels=var_labels, ioUtf8=True)
            sav_defs[section['name']] = {
                'sav_file': sav_file, 'sav_writer': sav_writer}

        index = 1
        indices = {}
        survey_name = self.survey.name
        for d in data:
            # decode mongo section names
            joined_export = dict_to_joined_export(d, index, indices,
                                                  survey_name)
            output = ExportBuilder.decode_mongo_encoded_section_names(
                joined_export)
            # attach meta fields (index, parent_index, parent_table)
            # output has keys for every section
            if survey_name not in output:
                output[survey_name] = {}
            output[survey_name][INDEX] = index
            output[survey_name][PARENT_INDEX] = -1
            for section in self.sections:
                # get data for this section and write to csv
                section_name = section['name']
                sav_def = sav_defs[section_name]
                fields = [
                    element['xpath'] for element in
                    section['elements']] + self.EXTRA_FIELDS
                sav_writer = sav_def['sav_writer']
                row = output.get(section_name, None)
                if type(row) == dict:
                    write_row(
                        self.pre_process_row(row, section),
                        sav_writer, fields)
                elif type(row) == list:
                    for child_row in row:
                        write_row(
                            self.pre_process_row(child_row, section),
                            sav_writer, fields)
            index += 1

        for section_name, sav_def in sav_defs.iteritems():
            sav_def['sav_writer'].closeSavFile(
                sav_def['sav_writer'].fh, mode='wb')

        # write zipfile
        with ZipFile(path, 'w') as zip_file:
            for section_name, sav_def in sav_defs.iteritems():
                sav_file = sav_def['sav_file']
                sav_file.seek(0)
                zip_file.write(
                    sav_file.name, "_".join(section_name.split("/")) + ".sav")

        # close files when we are done
        for section_name, sav_def in sav_defs.iteritems():
            sav_def['sav_file'].close()

    def get_fields(self, dataview, section, key):
        if dataview:
            return [element[key] for element in section['elements']
                    if [col for col in dataview.columns
                        if element[key].startswith(col)]] + self.EXTRA_FIELDS

        else:
            return [element[key] for element in
                    section['elements']] + self.EXTRA_FIELDS


def dict_to_flat_export(d, parent_index=0):
    pass


def md5hash(string):
    return hashlib.md5(string).hexdigest()


def get_export_options(options):
    export_options = {
        key: value for key, value in options.iteritems()
        if key in Export.EXPORT_OPTION_FIELDS}

    if EXPORT_QUERY_KEY in export_options:
        query_str = '{}'.format(export_options[EXPORT_QUERY_KEY])

        export_options[EXPORT_QUERY_KEY] = md5hash(query_str)

    return export_options


def generate_options_query(query, options):
    """
    Add option filters to Export query
    """
    query_with_filter = query
    for field in Export.EXPORT_OPTION_FIELDS:
        if field in options:
            field_value = options.get(field)

            if isinstance(field_value, bool):
                field_value = json.dumps(field_value)
            elif field == EXPORT_QUERY_KEY:
                query_str = str(format(field_value))

                field_value = '"{}"'.format(md5hash(query_str))
            else:
                field_value = '"{}"'.format(field_value)

            option_field_query = '"{}":{}'.format(field, field_value)
            query_with_filter = query_with_filter.filter(
                options__contains=option_field_query)

    return query_with_filter


def get_boolean_value(str_var, default=None):
    if isinstance(str_var, basestring) and \
            str_var.lower() in ['true', 'false']:
        return str_to_bool(str_var)

    return str_var if default else False


def generate_export(export_type, username, id_string, export_id=None,
                    options=None, xform=None):
    """
    Create appropriate export object given the export type.

    param: export_type
    params: username: logged in username
    params: id_string: xform id_string
    params: export_id: ID of export object associated with the request
    param: options: additional parameters required for the lookup.
        binary_select_multiples: boolean flag
        end: end offset
        ext: export extension type
        dataview_pk: dataview pk
        group_delimiter: "/" or "."
        query: filter_query for custom queries
        remove_group_name: boolean flag
        split_select_multiples: boolean flag
    """
    end = options.get("end")
    extension = options.get("extension", export_type)
    filter_query = options.get("query")
    remove_group_name = options.get("remove_group_name", False)
    start = options.get("start")

    export_type_func_map = {
        Export.XLS_EXPORT: 'to_xls_export',
        Export.CSV_EXPORT: 'to_flat_csv_export',
        Export.CSV_ZIP_EXPORT: 'to_zipped_csv',
        Export.SAV_ZIP_EXPORT: 'to_zipped_sav',
    }

    if xform is None:
        xform = XForm.objects.get(
            user__username__iexact=username, id_string__iexact=id_string)

    dataview = None
    if options.get("dataview_pk"):
        dataview = DataView.objects.get(pk=options.get("dataview_pk"))
        records = dataview.query_data(dataview, all_data=True)
    else:
        records = query_data(xform, query=filter_query, start=start, end=end)

    export_builder = ExportBuilder()

    export_builder.TRUNCATE_GROUP_TITLE = remove_group_name
    export_builder.GROUP_DELIMITER = options.get(
        "group_delimiter", DEFAULT_GROUP_DELIMITER
    )
    export_builder.SPLIT_SELECT_MULTIPLES = options.get(
        "split_select_multiples", True
    )
    export_builder.BINARY_SELECT_MULTIPLES = options.get(
        "binary_select_multiples", False
    )
    export_builder.INCLUDE_LABELS = options.get('include_labels', False)
    export_builder.INCLUDE_LABELS_ONLY = options.get(
        'include_labels_only', False
    )
    export_builder.set_survey(xform.data_dictionary().survey)

    temp_file = NamedTemporaryFile(suffix=("." + extension))

    # get the export function by export type
    func = getattr(export_builder, export_type_func_map[export_type])
    try:
        func.__call__(
            temp_file.name, records, username, id_string, filter_query,
            start=start, end=end, dataview=dataview, xform=xform
        )
    except NoRecordsFoundError:
        pass

    # generate filename
    basename = "%s_%s" % (
        id_string, datetime.now().strftime("%Y_%m_%d_%H_%M_%S"))

    if remove_group_name:
        # add 'remove group name' flag to filename
        basename = "{}-{}".format(basename, GROUPNAME_REMOVED_FLAG)
    if dataview:
        basename = "{}-{}".format(basename, DATAVIEW_EXPORT)

    filename = basename + "." + extension

    # check filename is unique
    while not Export.is_filename_unique(xform, filename):
        filename = increment_index_in_filename(filename)

    file_path = os.path.join(
        username,
        'exports',
        id_string,
        export_type,
        filename)

    # TODO: if s3 storage, make private - how will we protect local storage??
    storage = get_storage_class()()
    # seek to the beginning as required by storage classes
    temp_file.seek(0)
    export_filename = storage.save(file_path, File(temp_file, file_path))
    temp_file.close()

    dir_name, basename = os.path.split(export_filename)

    # get or create export object
    if export_id:
        export = Export.objects.get(id=export_id)
    else:
        export_options = get_export_options(options)
        export = Export(
            xform=xform, export_type=export_type, options=export_options)

    export.filedir = dir_name
    export.filename = basename
    export.internal_status = Export.SUCCESSFUL
    # do not persist exports that have a filter

    # if we should create a new export is true, we should not save it
    if start is None and end is None:
        export.save()
    return export


def should_create_new_export(xform,
                             export_type,
                             options,
                             request=None):
    """
    Function that determines whether to create a new export.
    param: xform
    param: export_type
    param: options: additional parameters required for the lookup.
        remove_group_name: boolean flag
        group_delimiter: "/" or "." with "/" as the default
        split_select_multiples: boolean flag
        binary_select_multiples: boolean flag
    params: request: Get params are used to determine if new export is required
    """
    split_select_multiples = options.get('split_select_multiples', True)

    if (request and (frozenset(request.GET.keys()) &
                     frozenset(['start', 'end', 'data_id']))) or\
            not split_select_multiples:
        return True

    export_query = Export.objects.filter(xform=xform, export_type=export_type)
    export_query = generate_options_query(export_query, options)

    if export_query.count() == 0 or\
       Export.exports_outdated(xform, export_type):
        return True

    return False


def newest_export_for(xform, export_type, options):
    """
    Retrieve the latest export given the following arguments:

    param: xform
    param: export_type
    param: options: additional parameters required for the lookup.
        remove_group_name: boolean flag
        group_delimiter: "/" or "." with "/" as the default
        split_select_multiples: boolean flag
        binary_select_multiples: boolean flag
    """

    export_query = Export.objects.filter(xform=xform, export_type=export_type)
    export_query = generate_options_query(export_query, options)

    return export_query.latest('created_on')


def increment_index_in_filename(filename):
    """
    filename should be in the form file.ext or file-2.ext - we check for the
    dash and index and increment appropriately
    """
    # check for an index i.e. dash then number then dot extension
    regex = re.compile(r"(.+?)\-(\d+)(\..+)")
    match = regex.match(filename)
    if match:
        basename = match.groups()[0]
        index = int(match.groups()[1]) + 1
        ext = match.groups()[2]
    else:
        index = 1
        # split filename from ext
        basename, ext = os.path.splitext(filename)
    new_filename = "%s-%d%s" % (basename, index, ext)
    return new_filename


def generate_attachments_zip_export(export_type, username, id_string,
                                    export_id=None, options=None,
                                    xform=None):
    """
    Generates zip export of attachments.

    param: export_type
    params: username: logged in username
    params: id_string: xform id_string
    params: export_id: ID of export object associated with the request
    param: options: additional parameters required for the lookup.
        ext: File extension of the generated export
    """
    extension = options.get("extension", export_type)

    if xform is None:
        xform = XForm.objects.get(user__username=username, id_string=id_string)
    attachments = Attachment.objects.filter(instance__xform=xform)
    basename = "%s_%s" % (id_string,
                          datetime.now().strftime("%Y_%m_%d_%H_%M_%S"))
    filename = basename + "." + extension
    file_path = os.path.join(
        username,
        'exports',
        id_string,
        export_type,
        filename)
    storage = get_storage_class()()
    zip_file = None

    try:
        zip_file = create_attachments_zipfile(attachments)

        try:
            temp_file = open(zip_file.name)
            export_filename = storage.save(
                file_path,
                File(temp_file, file_path))
        finally:
            temp_file.close()
    finally:
        zip_file and zip_file.close()

    dir_name, basename = os.path.split(export_filename)

    # get or create export object
    if(export_id):
        export = Export.objects.get(id=export_id)
    else:
        export_options = get_export_options(options)
        export = Export.objects.create(xform=xform,
                                       export_type=export_type,
                                       options=export_options)

    export.filedir = dir_name
    export.filename = basename
    export.internal_status = Export.SUCCESSFUL
    export.save()
    return export


def generate_kml_export(export_type, username, id_string, export_id=None,
                        options=None, xform=None):
    """
    Generates kml export for geographical data

    param: export_type
    params: username: logged in username
    params: id_string: xform id_string
    params: export_id: ID of export object associated with the request
    param: options: additional parameters required for the lookup.
        ext: File extension of the generated export
    """
    extension = options.get("extension", export_type)

    user = User.objects.get(username=username)
    if xform is None:
        xform = XForm.objects.get(user__username=username, id_string=id_string)
    response = render_to_response(
        'survey.kml', {'data': kml_export_data(id_string, user)})

    basename = "%s_%s" % (id_string,
                          datetime.now().strftime("%Y_%m_%d_%H_%M_%S"))
    filename = basename + "." + extension
    file_path = os.path.join(
        username,
        'exports',
        id_string,
        export_type,
        filename)

    storage = get_storage_class()()
    temp_file = NamedTemporaryFile(suffix=extension)
    temp_file.write(response.content)
    temp_file.seek(0)
    export_filename = storage.save(
        file_path,
        File(temp_file, file_path))
    temp_file.close()

    dir_name, basename = os.path.split(export_filename)

    # get or create export object
    if(export_id):
        export = Export.objects.get(id=export_id)
    else:
        export_options = get_export_options(options)
        export = Export.objects.create(xform=xform,
                                       export_type=export_type,
                                       options=export_options)

    export.filedir = dir_name
    export.filename = basename
    export.internal_status = Export.SUCCESSFUL
    export.save()

    return export


def kml_export_data(id_string, user):
    # TODO resolve circular import
    from onadata.apps.viewer.models.data_dictionary import DataDictionary
    dd = DataDictionary.objects.get(id_string=id_string, user=user)
    instances = Instance.objects.filter(
        xform__user=user, xform__id_string=id_string, geom__isnull=False
    ).order_by('id')
    data_for_template = []

    labels = {}

    def cached_get_labels(xpath):
        if xpath in labels.keys():
            return labels[xpath]
        labels[xpath] = dd.get_label(xpath)
        return labels[xpath]

    for instance in instances:
        # read the survey instances
        data_for_display = instance.get_dict()
        xpaths = data_for_display.keys()
        xpaths.sort(cmp=instance.xform.data_dictionary().get_xpath_cmp())
        label_value_pairs = [
            (cached_get_labels(xpath), data_for_display[xpath]) for xpath in
            xpaths if not xpath.startswith(u"_")]
        table_rows = ['<tr><td>%s</td><td>%s</td></tr>' % (k, v) for k, v
                      in label_value_pairs]
        img_urls = image_urls(instance)
        img_url = img_urls[0] if img_urls else ""
        point = instance.point

        if point:
            data_for_template.append({
                'name': id_string,
                'id': instance.id,
                'lat': point.y,
                'lng': point.x,
                'image_urls': img_urls,
                'table': '<table border="1"><a href="#"><img width="210" '
                         'class="thumbnail" src="%s" alt=""></a>%s'
                         '</table>' % (img_url, ''.join(table_rows))})

    return data_for_template


def generate_osm_export(export_type, username, id_string, export_id=None,
                        options=None, xform=None):
    """
    Generates osm export for OpenStreetMap data

    param: export_type
    params: username: logged in username
    params: id_string: xform id_string
    params: export_id: ID of export object associated with the request
    param: options: additional parameters required for the lookup.
        ext: File extension of the generated export
    """

    extension = options.get("extension", export_type)

    if xform is None:
        xform = XForm.objects.get(user__username=username, id_string=id_string)
    osm_list = OsmData.objects.filter(instance__xform=xform)
    content = get_combined_osm(osm_list)

    basename = "%s_%s" % (id_string,
                          datetime.now().strftime("%Y_%m_%d_%H_%M_%S"))
    filename = basename + "." + extension
    file_path = os.path.join(
        username,
        'exports',
        id_string,
        export_type,
        filename)

    storage = get_storage_class()()
    temp_file = NamedTemporaryFile(suffix=extension)
    temp_file.write(content)
    temp_file.seek(0)
    export_filename = storage.save(
        file_path,
        File(temp_file, file_path))
    temp_file.close()

    dir_name, basename = os.path.split(export_filename)

    # get or create export object
    if(export_id):
        export = Export.objects.get(id=export_id)
    else:
        export_options = get_export_options(options)
        export = Export.objects.create(xform=xform,
                                       export_type=export_type,
                                       options=export_options)

    export.filedir = dir_name
    export.filename = basename
    export.internal_status = Export.SUCCESSFUL
    export.save()

    return export


def _get_records(instances):
    return [clean_keys_of_slashes(instance)
            for instance in instances]


def clean_keys_of_slashes(record):
    """
    Replaces the slashes found in a dataset keys with underscores
    :param record: list containing a couple of dictionaries
    :return: record with keys without slashes
    """
    for key in record.keys():
        value = record[key]
        if '/' in key:
            # replace with _
            record[key.replace('/', '_')]\
                = record.pop(key)
        # Check if the value is a list containing nested dict and apply same
        if value:
            if isinstance(value, list) and isinstance(value[0], dict):
                for v in value:
                    clean_keys_of_slashes(v)

    return record


def _get_server_from_metadata(xform, meta, token):
    report_templates = MetaData.external_export(xform)

    if meta:
        try:
            int(meta)
        except ValueError:
            raise Exception(u"Invalid metadata pk {0}".format(meta))

        # Get the external server from the metadata
        result = report_templates.get(pk=meta)
        server = result.external_export_url
        name = result.external_export_name
    elif token:
        server = token
        name = None
    else:
        # Take the latest value in the metadata
        if not report_templates:
            raise Exception(
                u"Could not find the template token: Please upload template.")

        server = report_templates[0].external_export_url
        name = report_templates[0].external_export_name

    return server, name


def generate_external_export(export_type, username, id_string, export_id=None,
                             options=None, xform=None):
    """
    Generates external export using ONA data through an external service.

    param: export_type
    params: username: logged in username
    params: id_string: xform id_string
    params: export_id: ID of export object associated with the request
    param: options: additional parameters required for the lookup.
        data_id: instance id
        query: filter_query for custom queries
        meta: metadata associated with external export
        token: authentication key required by external service
    """
    data_id = options.get("data_id")
    filter_query = options.get("query")
    meta = options.get("meta")
    token = options.get("token")

    if xform is None:
        xform = XForm.objects.get(
            user__username__iexact=username, id_string__iexact=id_string)
    user = User.objects.get(username=username)

    server, name = _get_server_from_metadata(xform, meta, token)

    # dissect the url
    parsed_url = urlparse(server)

    token = parsed_url.path[5:]

    ser = parsed_url.scheme + '://' + parsed_url.netloc

    # Get single submission data
    if data_id:
        inst = Instance.objects.filter(xform__user=user,
                                       xform__id_string=id_string,
                                       deleted_at=None,
                                       pk=data_id)

        instances = [inst[0].get_dict() if inst else {}]
    else:
        instances = query_data(xform, query=filter_query)

    records = _get_records(instances)

    status_code = 0

    if records and server:
        try:
            client = Client(ser)
            response = client.xls.create(token, json.dumps(records))

            if hasattr(client.xls.conn, 'last_response'):
                status_code = client.xls.conn.last_response.status_code
        except Exception as e:
            raise J2XException(
                u"J2X client could not generate report. Server -> {0},"
                u" Error-> {1}".format(server, e)
            )
    else:
        if not server:
            raise J2XException(u"External server not set")
        elif not records:
            raise J2XException(
                u"No record to export. Form -> {0}".format(id_string)
            )

    # get or create export object
    if export_id:
        export = Export.objects.get(id=export_id)
    else:
        export_options = get_export_options(options)
        export = Export.objects.create(xform=xform,
                                       export_type=export_type,
                                       options=export_options)

    export.export_url = response
    if status_code == 201:
        export.internal_status = Export.SUCCESSFUL
        export.filename = name + '-' + response[5:] if name else response[5:]
        export.export_url = ser + response
    else:
        export.internal_status = Export.FAILED

    export.save()

    return export


def upload_template_for_external_export(server, file_obj):

    try:
        client = Client(server)
        response = client.template.create(template_file=file_obj)

        if hasattr(client.template.conn, 'last_response'):
            status_code = client.template.conn.last_response.status_code
    except Exception as e:
        response = str(e)
        status_code = 500

    return str(status_code) + '|' + response


def parse_request_export_options(request):
    """
    Parse export options in the request object into values returned in a
    list. The list represents a boolean for whether the group name should be
    removed, the group delimiter, and a boolean for whether select multiples
    should be split.
    """
    boolean_list = ['true', 'false']
    options = {}
    params = request.QUERY_PARAMS
    remove_group_name = params.get('remove_group_name') and \
        params.get('remove_group_name').lower()
    do_not_split_select_multiples = params.get(
        'do_not_split_select_multiples')
    include_labels = params.get('include_labels', None)
    include_labels_only = params.get('include_labels_only', None)

    if include_labels is not None:
        options['include_labels'] = str_to_bool(include_labels)

    if include_labels_only is not None:
        options['include_labels_only'] = str_to_bool(include_labels_only)

    if remove_group_name in boolean_list:
        options["remove_group_name"] = str_to_bool(remove_group_name)
    else:
        options["remove_group_name"] = False

    if params.get("group_delimiter") in ['.', DEFAULT_GROUP_DELIMITER]:
        options['group_delimiter'] = params.get("group_delimiter")
    else:
        options['group_delimiter'] = DEFAULT_GROUP_DELIMITER

    options['split_select_multiples'] = not do_not_split_select_multiples

    return options
