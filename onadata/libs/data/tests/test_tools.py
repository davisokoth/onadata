from datetime import datetime, timedelta
import os

from mock import patch
from nose.tools import raises

from onadata.apps.main.tests.test_base import TestBase
from onadata.libs.data.query import get_form_submissions_grouped_by_field


class TestTools(TestBase):

    def setUp(self):
        super(self.__class__, self).setUp()
        self._create_user_and_login()
        self._publish_transportation_form()

    @patch('django.utils.timezone.now')
    def test_get_form_submissions_grouped_by_field(self, mock_time):
        mock_time.return_value = datetime.now()
        self._make_submissions()

        count_key = 'count'
        fields = ['_submission_time', '_xform_id_string']

        count = len(self.xform.surveys.all())

        for field in fields:
            result = get_form_submissions_grouped_by_field(
                self.xform, field)[0]

            self.assertEqual([field, count_key], sorted(result.keys()))
            self.assertEqual(result[count_key], count)

    @patch('onadata.apps.odk_logger.models.instance.submission_time')
    def test_get_form_submissions_grouped_by_field_datetime_to_date(
            self, mock_time):
        now = datetime(2014, 01, 01)
        times = [now, now + timedelta(seconds=1), now + timedelta(seconds=2),
                 now + timedelta(seconds=3)]
        mock_time.side_effect = times
        self._make_submissions()

        count_key = 'count'
        fields = ['_submission_time']

        count = len(self.xform.surveys.all())

        for field in fields:
            result = get_form_submissions_grouped_by_field(
                self.xform, field)[0]

            self.assertEqual([field, count_key], sorted(result.keys()))
            self.assertEqual(result[field], str(now.date()))
            self.assertEqual(result[count_key], count)

    @patch('django.utils.timezone.now')
    def test_get_form_submissions_two_xforms(self, mock_time):
        mock_time.return_value = datetime.now()
        self._make_submissions()
        self._publish_xls_file(os.path.join(
            "fixtures",
            "gps", "gps.xls"))

        first_xform = self.xform
        self.xform = self.user.xforms.all().order_by('-pk')[0]

        self._make_submission(os.path.join(
            'onadata', 'apps', 'main', 'tests', 'fixtures', 'gps',
            'instances', 'gps_1980-01-23_20-52-08.xml'))

        count_key = 'count'
        fields = ['_submission_time', '_xform_id_string']

        count = len(self.xform.surveys.all())

        for field in fields:
            result = get_form_submissions_grouped_by_field(
                self.xform, field)[0]

            self.assertEqual([field, count_key], sorted(result.keys()))
            self.assertEqual(result[count_key], count)

        count = len(first_xform.surveys.all())

        for field in fields:
            result = get_form_submissions_grouped_by_field(
                first_xform, field)[0]

            self.assertEqual([field, count_key], sorted(result.keys()))
            self.assertEqual(result[count_key], count)

    @patch('django.utils.timezone.now')
    def test_get_form_submissions_xform_no_submissions(self, mock_time):
        mock_time.return_value = datetime.now()
        self._make_submissions()
        self._publish_xls_file(os.path.join(
            "fixtures",
            "gps", "gps.xls"))

        self.xform = self.user.xforms.all().order_by('-pk')[0]

        fields = ['_submission_time', '_xform_id_string']

        count = len(self.xform.surveys.all())
        self.assertEqual(count, 0)
        for field in fields:
            result = get_form_submissions_grouped_by_field(
                self.xform, field)
            self.assertEqual(result, [])

    @patch('django.utils.timezone.now')
    def test_get_form_submissions_grouped_by_field_sets_name(self, mock_time):
        mock_time.return_value = datetime.now()
        self._make_submissions()

        count_key = 'count'
        fields = ['_submission_time', '_xform_id_string']
        name = '_my_name'

        xform = self.user.xforms.all()[0]
        count = len(xform.surveys.all())

        for field in fields:
            result = get_form_submissions_grouped_by_field(
                xform, field, name)[0]

            self.assertEqual([name, count_key], sorted(result.keys()))
            self.assertEqual(result[count_key], count)

    @raises(ValueError)
    def test_get_form_submissions_grouped_by_field_bad_field(self):
        self._make_submissions()

        field = '_bad_field'
        xform = self.user.xforms.all()[0]

        get_form_submissions_grouped_by_field(xform, field)