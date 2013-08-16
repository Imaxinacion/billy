from __future__ import unicode_literals
import datetime
import decimal

import transaction
from freezegun import freeze_time

from billy.tests.helper import ModelTestCase


@freeze_time('2013-08-16')
class TestPlanModel(ModelTestCase):

    def setUp(self):
        from billy.models.company import CompanyModel
        super(TestPlanModel, self).setUp()
        # build the basic scenario for plan model
        self.company_model = CompanyModel(self.session)
        with transaction.manager:
            self.company_guid = self.company_model.create_company('my_secret_key')

    def make_one(self, *args, **kwargs):
        from billy.models.plan import PlanModel
        return PlanModel(*args, **kwargs)

    def test_get_plan(self):
        model = self.make_one(self.session)

        plan = model.get_plan_by_guid('PL_NON_EXIST')
        self.assertEqual(plan, None)

        with self.assertRaises(KeyError):
            model.get_plan_by_guid('PL_NON_EXIST', raise_error=True)

        with transaction.manager:
            guid = model.create_plan(
                company_guid=self.company_guid,
                plan_type=model.TYPE_CHARGE,
                name='name',
                amount=99.99,
                frequency=model.FREQ_WEEKLY,
            )
            model.delete_plan(guid)

        with self.assertRaises(KeyError):
            model.get_plan_by_guid(guid, raise_error=True)

        plan = model.get_plan_by_guid(guid, ignore_deleted=False, raise_error=True)
        self.assertEqual(plan.guid, guid)

    def test_create_plan(self):
        model = self.make_one(self.session)
        name = 'monthly billing to user John'
        amount = decimal.Decimal('5566.77')
        frequency = model.FREQ_MONTHLY
        plan_type = model.TYPE_CHARGE
        external_id = '5566_GOOD_BROTHERS'
        description = 'This is a long description'

        with transaction.manager:
            guid = model.create_plan(
                company_guid=self.company_guid,
                plan_type=plan_type,
                name=name,
                amount=amount,
                frequency=frequency,
                external_id=external_id,
                description=description,
            )

        now = datetime.datetime.utcnow()

        plan = model.get_plan_by_guid(guid)
        self.assertEqual(plan.guid, guid)
        self.assert_(plan.guid.startswith('PL'))
        self.assertEqual(plan.company_guid, self.company_guid)
        self.assertEqual(plan.name, name)
        self.assertEqual(plan.amount, amount)
        self.assertEqual(plan.frequency, frequency)
        self.assertEqual(plan.plan_type, plan_type)
        self.assertEqual(plan.external_id, external_id)
        self.assertEqual(plan.description, description)
        self.assertEqual(plan.deleted, False)
        self.assertEqual(plan.created_at, now)
        self.assertEqual(plan.updated_at, now)

    def test_create_plan_with_wrong_frequency(self):
        model = self.make_one(self.session)

        with self.assertRaises(ValueError):
            model.create_plan(
                company_guid=self.company_guid,
                plan_type=model.TYPE_CHARGE,
                name=None,
                amount=999,
                frequency=999,
            )

    def test_create_plan_with_wrong_type(self):
        model = self.make_one(self.session)

        with self.assertRaises(ValueError):
            model.create_plan(
                company_guid=self.company_guid,
                plan_type=999,
                name=None,
                amount=999,
                frequency=model.FREQ_DAILY,
            )

    def test_update_plan(self):
        model = self.make_one(self.session)

        with transaction.manager:
            guid = model.create_plan(
                company_guid=self.company_guid,
                plan_type=model.TYPE_CHARGE,
                name='old name',
                amount=99.99,
                frequency=model.FREQ_WEEKLY,
                description='old description',
                external_id='old external id',
            )

        plan = model.get_plan_by_guid(guid)
        name = 'new name'
        description = 'new description'
        external_id = 'new external id'

        with transaction.manager:
            model.update_plan(
                guid=guid,
                name=name,
                description=description,
                external_id=external_id,
            )

        plan = model.get_plan_by_guid(guid)
        self.assertEqual(plan.name, name)
        self.assertEqual(plan.description, description)
        self.assertEqual(plan.external_id, external_id)

    def test_update_plan_updated_at(self):
        model = self.make_one(self.session)

        with transaction.manager:
            guid = model.create_plan(
                company_guid=self.company_guid,
                plan_type=model.TYPE_CHARGE,
                name='evil gangster charges protection fee from Tom weekly',
                amount=99.99,
                frequency=model.FREQ_WEEKLY,
            )

        plan = model.get_plan_by_guid(guid)
        created_at = plan.created_at
        name = 'new plan name'

        # advanced the current date time
        with freeze_time('2013-08-16 07:00:01'):
            with transaction.manager:
                model.update_plan(
                    guid=guid,
                    name=name,
                )
            updated_at = datetime.datetime.utcnow()

        plan = model.get_plan_by_guid(guid)
        self.assertEqual(plan.name, name)
        self.assertEqual(plan.updated_at, updated_at)
        self.assertEqual(plan.created_at, created_at)

        # advanced the current date time even more
        with freeze_time('2013-08-16 08:35:40'):
            # this should update the updated_at field only
            with transaction.manager:
                model.update_plan(guid)
            updated_at = datetime.datetime.utcnow()

        plan = model.get_plan_by_guid(guid)
        self.assertEqual(plan.name, name)
        self.assertEqual(plan.updated_at, updated_at)
        self.assertEqual(plan.created_at, created_at)

        # make sure passing wrong argument will raise error
        with self.assertRaises(TypeError):
            model.update_plan(guid, wrong_arg=True, neme='john')

    def test_delete_plan(self):
        model = self.make_one(self.session)

        with transaction.manager:
            guid = model.create_plan(
                company_guid=self.company_guid,
                plan_type=model.TYPE_CHARGE,
                name='name',
                amount=99.99,
                frequency=model.FREQ_WEEKLY,
            )
            model.delete_plan(guid)

        plan = model.get_plan_by_guid(guid)
        self.assertEqual(plan, None)

        plan = model.get_plan_by_guid(guid, ignore_deleted=False)
        self.assertEqual(plan.deleted, True)
