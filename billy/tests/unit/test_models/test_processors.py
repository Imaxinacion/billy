from __future__ import unicode_literals
import datetime
import unittest

import mock
import balanced
import transaction as db_transaction
from freezegun import freeze_time

from billy.models.transaction import DuplicateEventError
from billy.models.processors.base import PaymentProcessor
from billy.models.processors.balanced_payments import InvalidURIFormat
from billy.models.processors.balanced_payments import InvalidFundingInstrument
from billy.models.processors.balanced_payments import InvalidCallbackPayload
from billy.models.processors.balanced_payments import BalancedProcessor
from billy.tests.unit.helper import ModelTestCase
from billy.utils.generic import utc_now


class TestPaymentProcessorModel(unittest.TestCase):

    def test_base_processor(self):
        processor = PaymentProcessor()
        for method_name in [
            'configure_api_key',
            'callback',
            'register_callback',
            'validate_customer',
            'validate_funding_instrument',
            'create_customer',
            'prepare_customer',
            'debit',
            'credit',
            'refund',
        ]:
            with self.assertRaises(NotImplementedError):
                method = getattr(processor, method_name)
                if method_name in ['callback', 'register_callback']:
                    method(None, None)
                else:
                    method(None)


@freeze_time('2013-08-16')
class TestBalancedProcessorModel(ModelTestCase):

    def setUp(self):
        super(TestBalancedProcessorModel, self).setUp()
        # build the basic scenario for transaction model
        with db_transaction.manager:
            self.company = self.company_model.create('my_secret_key')
            self.plan = self.plan_model.create(
                company=self.company,
                plan_type=self.plan_model.types.DEBIT,
                amount=10,
                frequency=self.plan_model.frequencies.MONTHLY,
            )
            self.customer = self.customer_model.create(
                company=self.company,
                processor_uri='MOCK_BALANCED_CUSTOMER_URI',
            )
            self.subscription = self.subscription_model.create(
                customer=self.customer,
                plan=self.plan,
                funding_instrument_uri='/v1/cards/tester',
            )
            self.invoice = self.invoice_model.create(
                customer=self.customer,
                amount=100,
            )
            self.transaction = self.transaction_model.create(
                invoice=self.invoice,
                transaction_type=self.transaction_model.types.DEBIT,
                amount=10,
                funding_instrument_uri='/v1/cards/tester',
            )

    def make_one(self, configure_api_key=True, *args, **kwargs):
        processor = BalancedProcessor(*args, **kwargs)
        if configure_api_key:
            processor.configure_api_key('MOCK_API_KEY')
        return processor

    def make_event(
        self,
        event_id='EV_MOCK_EVENT_ID',
        transaction_guid=None,
        occurred_at=None,
        status='succeeded',
    ):
        """Make a mock Balanced.Event instance and return

        """
        if transaction_guid is None:
            transaction_guid = self.transaction.guid
        if occurred_at is None:
            occurred_at = utc_now()
        event = mock.Mock(
            id=event_id,
            occurred_at=occurred_at,
            entity=dict(
                entity_type=[dict(
                    meta={'billy.transaction_guid': transaction_guid},
                    status=status,
                )],
                links=[],
            )
        )
        return event

    def make_callback_payload(self):
        return dict(
            id='MOCK_EVENT_GUID',
            type='debit.updated',
        )

    def test_callback(self):
        event = self.make_event()
        Event = mock.Mock()
        Event.fetch.return_value = event

        payload = self.make_callback_payload()
        processor = self.make_one(event_cls=Event)
        update_db = processor.callback(self.company, payload)
        update_db(self.model_factory)

        Event.fetch.assert_called_once_with('/v1/events/MOCK_EVENT_GUID')
        self.assertEqual(self.transaction.status, self.transaction_model.statuses.SUCCEEDED)
        events = list(self.transaction.events)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].status, self.transaction_model.statuses.SUCCEEDED)
        self.assertEqual(events[0].occurred_at, event.occurred_at)

    def test_callback_without_meta_guid(self):
        event = self.make_event()
        event.entity['entity_type'][0]['meta'] = {}
        Event = mock.Mock()
        Event.fetch.return_value = event

        payload = self.make_callback_payload()
        processor = self.make_one(event_cls=Event)
        update_db = processor.callback(self.company, payload)
        self.assertEqual(update_db, None)

    def _do_callback(self, event_id, status, occurred_at, company=None):
        if company is None:
            company = self.company
        event = self.make_event(
            event_id=event_id,
            status=status,
            occurred_at=occurred_at,
        )
        Event = mock.Mock()
        Event.fetch.return_value = event

        payload = self.make_callback_payload()
        processor = self.make_one(event_cls=Event)
        update_db = processor.callback(company, payload)
        with db_transaction.manager:
            update_db(self.model_factory)

    def test_callback_with_duplicate_event(self):
        now = utc_now()
        self._do_callback('EV_ID_1', 'succeeded', now)
        with self.assertRaises(DuplicateEventError):
            self._do_callback('EV_ID_1', 'succeeded', now)

    def test_callback_only_latest_event_affects_status(self):
        time1 = utc_now()
        time2 = time1 + datetime.timedelta(seconds=10)
        time3 = time1 + datetime.timedelta(seconds=20)
        ts = self.transaction_model.statuses
        vs = self.invoice_model.statuses

        def assert_status(
            ev_id, status, time, expected_iv_status, expected_tx_status
        ):
            self._do_callback(ev_id, status, time)
            self.assertEqual(self.transaction.status, expected_tx_status)
            self.assertEqual(self.invoice.status, expected_iv_status)

        assert_status('EV_ID_1', 'pending', time1, vs.PROCESSING, ts.PENDING)
        assert_status('EV_ID_3', 'failed', time3, vs.FAILED, ts.FAILED)
        # this is the point, EV_ID_2 arrived later than EV_ID_3, but its
        # occurred_at time is earlier than EV_ID3, so it should never affect
        # the status of transaction and invoice
        assert_status('EV_ID_2', 'succeeded', time2, vs.FAILED, ts.FAILED)

        # ensure events are generated correctly and in right order
        for event, (expected_ev_id, expected_status, expected_time) in zip(
            self.transaction.events, [
                ('EV_ID_3', ts.FAILED, time3),
                ('EV_ID_2', ts.SUCCEEDED, time2),
                ('EV_ID_1', ts.PENDING, time1),
            ]
        ):
            self.assertEqual(event.processor_id, expected_ev_id)
            self.assertEqual(event.status, expected_status)
            self.assertEqual(event.occurred_at, expected_time)

    def test_callback_with_other_company(self):
        with db_transaction.manager:
            other_company = self.company_model.create('MOCK_PROCESSOR_KEY')
        with self.assertRaises(InvalidCallbackPayload):
            self._do_callback('EVID', 'succeeded', utc_now(), other_company)

    def test_callback_with_not_exist_transaction(self):
        event = self.make_event(transaction_guid='NOT_EXIST_GUID')
        Event = mock.Mock()
        Event.fetch.return_value = event
        payload = self.make_callback_payload()
        processor = self.make_one(event_cls=Event)
        with self.assertRaises(InvalidCallbackPayload):
            update_db = processor.callback(self.company, payload)
            update_db(self.model_factory)

    def test_register_callback(self):
        url = 'http://foobar.com/callback'
        Callback = mock.Mock()
        processor = self.make_one(callback_cls=Callback)
        processor.register_callback(self.company, url)
        Callback.assert_called_once_with(url=url)

    def test_validate_customer(self):
        # mock class
        BalancedCustomer = mock.Mock()
        BalancedCustomer.fetch.return_value = mock.Mock(uri='MOCK_CUSTOMER_URI')

        processor = self.make_one(customer_cls=BalancedCustomer)
        result = processor.validate_customer('/v1/customers/xxx')
        self.assertTrue(result)

        BalancedCustomer.fetch.assert_called_once_with('/v1/customers/xxx')

    def test_validate_customer_with_invalid_uri(self):
        processor = self.make_one()
        with self.assertRaises(InvalidURIFormat):
            processor.validate_customer('CUXXXXXXXX')

    def test_validate_funding_instrument(self):
        # mock class
        Card = mock.Mock()
        Card.fetch.return_value = mock.Mock(
            href='MOCK_FUNDING_INSTRUMENT_URI',
        )

        processor = self.make_one(card_cls=Card)
        result = processor.validate_funding_instrument('/v1/cards/xxx')
        self.assertTrue(result)

        Card.fetch.assert_called_once_with('/v1/cards/xxx')

        BankAccount = mock.Mock()
        BankAccount.fetch.return_value = mock.Mock(
            href='MOCK_FUNDING_INSTRUMENT_URI',
        )

        processor = self.make_one(bank_account_cls=BankAccount)
        result = processor.validate_funding_instrument('/v1/bank_accounts/xxx')
        self.assertTrue(result)

        BankAccount.fetch.assert_called_once_with('/v1/bank_accounts/xxx')

    def test_validate_funding_instrument_with_invalid_card(self):
        # mock class
        Card = mock.Mock()
        Card.fetch.side_effect = balanced.exc.BalancedError('Boom')
        processor = self.make_one(card_cls=Card)
        with self.assertRaises(InvalidFundingInstrument):
            processor.validate_funding_instrument('/v1/cards/invalid_card')
        with self.assertRaises(InvalidFundingInstrument):
            processor.validate_funding_instrument('/v1/foobar/invalid_card')

    def test_validate_funding_instrument_with_invalid_uri(self):
        processor = self.make_one()
        with self.assertRaises(InvalidURIFormat):
            processor.validate_funding_instrument('CCXXXXXXXXX')

    def test_create_customer(self):
        self.customer.processor_uri = None

        # mock instance
        balanced_customer = mock.Mock()
        balanced_customer.save.return_value = mock.Mock(href='MOCK_CUSTOMER_URI')
        # mock class
        BalancedCustomer = mock.Mock()
        BalancedCustomer.return_value = balanced_customer

        processor = self.make_one(customer_cls=BalancedCustomer)
        customer_id = processor.create_customer(self.customer)
        self.assertEqual(customer_id, 'MOCK_CUSTOMER_URI')

        # make sure the customer is created correctly
        BalancedCustomer.assert_called_once_with(**{
            'meta.billy.customer_guid': self.customer.guid,
        })
        balanced_customer.save.assert_called_once_with()

    def test_prepare_customer_with_card(self):
        # mock instance
        customer = mock.Mock()
        card = mock.Mock()
        # mock class
        Customer = mock.Mock()
        Customer.fetch.return_value = customer
        Card = mock.Mock()
        Card.fetch.return_value = card

        href = '/v1/cards/my_card'
        processor = self.make_one(customer_cls=Customer, card_cls=Card)
        processor.prepare_customer(self.customer, href)
        # make sure the customer fetch method is called
        Customer.fetch.assert_called_once_with(self.customer.processor_uri)
        # make sure the card fetch method is called
        Card.fetch.assert_called_once_with(href)
        # make sure card is assoicated correctly
        card.associate_to_customer.assert_called_once_with(customer)

    def test_prepare_customer_with_bank_account(self):
        # mock instance
        customer = mock.Mock()
        bank_account = mock.Mock()
        # mock class
        Customer = mock.Mock()
        Customer.fetch.return_value = customer
        BankAccount = mock.Mock()
        BankAccount.fetch.return_value = bank_account

        href = '/v1/bank_accounts/my_account'
        processor = self.make_one(customer_cls=Customer, bank_account_cls=BankAccount)
        processor.prepare_customer(self.customer, href)
        # make sure the customer fetch method is called
        Customer.fetch.assert_called_once_with(self.customer.processor_uri)
        BankAccount.fetch.assert_called_once_with(href)
        # make sure card is associated correctly
        bank_account.associate_to_customer.assert_called_once_with(customer)

    def test_prepare_customer_with_none_funding_instrument_uri(self):
        # mock instance
        balanced_customer = mock.Mock()
        # mock class
        BalancedCustomer = mock.Mock()
        BalancedCustomer.fetch.return_value = balanced_customer

        processor = self.make_one(customer_cls=BalancedCustomer)
        processor.prepare_customer(self.customer, None)

        # make sure add_card and add_bank_account will not be called
        self.assertFalse(balanced_customer.add_card.called, 0)
        self.assertFalse(balanced_customer.add_bank_account.called, 0)

    def test_prepare_customer_with_bad_funding_instrument_uri(self):
        # mock instance
        balanced_customer = mock.Mock()
        # mock class
        BalancedCustomer = mock.Mock()
        BalancedCustomer.fetch.return_value = balanced_customer

        processor = self.make_one(customer_cls=BalancedCustomer)
        with self.assertRaises(ValueError):
            processor.prepare_customer(self.customer, '/v1/bitcoin/12345')

    def _test_operation(
        self,
        op_cls_name,
        fi_cls_name,
        api_method_name,
        extra_api_kwargs,
    ):
        tx_model = self.transaction_model
        with db_transaction.manager:
            transaction = tx_model.create(
                invoice=self.invoice,
                transaction_type=tx_model.types.DEBIT,
                amount=10,
                funding_instrument_uri='/v1/cards/tester',
                appears_on_statement_as='hello baby',
            )
            self.customer_model.update(
                customer=self.customer,
                processor_uri='MOCK_BALANCED_CUSTOMER_URI',
            )

        # mock page
        page = mock.Mock()
        page.one.side_effect = balanced.exc.NoResultFound
        # mock resource
        resource = mock.Mock(
            href='MOCK_BALANCED_RESOURCE_URI',
            status='succeeded',
        )
        # mock funding instrument instance
        funding_instrument = mock.Mock()
        api_method = getattr(funding_instrument, api_method_name)
        api_method.return_value = resource
        api_method.__name__ = api_method_name
        # mock funding instrumnet
        FundingInstrument = mock.Mock()
        FundingInstrument.fetch.return_value = funding_instrument
        # mock resource class
        Resource = mock.Mock()
        Resource.query.filter.return_value = page

        processor = self.make_one(**{
            op_cls_name: Resource,
            fi_cls_name: FundingInstrument,

        })
        method = getattr(processor, api_method_name)
        result = method(transaction)
        self.assertEqual(result['processor_uri'], 'MOCK_BALANCED_RESOURCE_URI')
        self.assertEqual(result['status'],
                         self.transaction_model.statuses.SUCCEEDED)
        # make sure query is made correctly
        expected_kwargs = {'meta.billy.transaction_guid': transaction.guid}
        Resource.query.filter.assert_called_once_with(**expected_kwargs)
        # make sure the operation method is called properly
        expected_kwargs = dict(
            amount=transaction.amount,
            meta={'billy.transaction_guid': transaction.guid},
            description=(
                'Generated by Billy from invoice {}'.format(self.invoice.guid)
            ),
            appears_on_statement_as='hello baby',
        )
        expected_kwargs.update(extra_api_kwargs)

        api_method = getattr(funding_instrument, api_method_name)
        api_method.assert_called_once_with(**expected_kwargs)

    def _test_operation_with_created_record(
        self,
        cls_name,
        api_method_name,
    ):
        tx_model = self.transaction_model
        with db_transaction.manager:
            transaction = tx_model.create(
                invoice=self.invoice,
                transaction_type=tx_model.types.DEBIT,
                amount=10,
                funding_instrument_uri='/v1/cards/tester',
            )

        # mock resource
        resource = mock.Mock(
            href='MOCK_BALANCED_RESOURCE_URI',
            status='succeeded',
        )
        # mock page
        page = mock.Mock()
        page.one.return_value = resource
        # mock customer instance
        customer = mock.Mock()
        api_method = getattr(customer, api_method_name)
        api_method.return_value = resource
        api_method.__name__ = api_method_name
        # mock customer class
        Customer = mock.Mock()
        Customer.fetch.return_value = customer
        # mock resource class
        Resource = mock.Mock()
        Resource.query.filter.return_value = page

        processor = self.make_one(
            customer_cls=Customer,
            **{cls_name: Resource}
        )
        method = getattr(processor, api_method_name)
        result = method(transaction)
        self.assertEqual(result['processor_uri'], 'MOCK_BALANCED_RESOURCE_URI')
        self.assertEqual(result['status'],
                         self.transaction_model.statuses.SUCCEEDED)

        # make sure the api method is not called
        self.assertFalse(Customer.fetch.called)
        self.assertFalse(api_method.called)
        # make sure query is made correctly
        expected_kwargs = {'meta.billy.transaction_guid': transaction.guid}
        Resource.query.filter.assert_called_once_with(**expected_kwargs)

    def test_debit(self):
        self._test_operation(
            op_cls_name='debit_cls',
            fi_cls_name='card_cls',
            api_method_name='debit',
            extra_api_kwargs=dict(source='/v1/cards/tester'),
        )

    def test_debit_with_created_record(self):
        self._test_operation_with_created_record(
            cls_name='debit_cls',
            api_method_name='debit',
        )

    def test_credit(self):
        self._test_operation(
            op_cls_name='credit_cls',
            fi_cls_name='card_cls',
            api_method_name='credit',
            extra_api_kwargs=dict(destination='/v1/cards/tester'),
        )

    def test_credit_with_created_record(self):
        self._test_operation_with_created_record(
            cls_name='credit_cls',
            api_method_name='credit',
        )

    def _create_refund_transaction(self):
        tx_model = self.transaction_model
        with db_transaction.manager:
            charge_transaction = tx_model.create(
                invoice=self.invoice,
                transaction_type=tx_model.types.DEBIT,
                amount=100,
                funding_instrument_uri='/v1/cards/tester',
            )
            charge_transaction.submit_status = tx_model.submit_statuses.DONE
            charge_transaction.processor_uri = 'MOCK_BALANCED_DEBIT_URI'
            self.session.flush()

            transaction = tx_model.create(
                invoice=self.invoice,
                transaction_type=tx_model.types.REFUND,
                reference_to=charge_transaction,
                amount=56,
                appears_on_statement_as='hello baby',
            )
        return transaction

    def test_refund(self):
        transaction = self._create_refund_transaction()

        # mock page
        page = mock.Mock()
        page.one.side_effect = balanced.exc.NoResultFound
        # mock debit instance
        debit = mock.Mock()
        debit.refund.return_value = mock.Mock(
            href='MOCK_REFUND_URI',
            status='succeeded',
        )
        debit.refund.__name__ = 'refund'
        # mock customer class
        Customer = mock.Mock()
        Customer.fetch.return_value = mock.Mock()
        # mock refund class
        Refund = mock.Mock()
        Refund.query.filter.return_value = page
        # mock debit class
        Debit = mock.Mock()
        Debit.fetch.return_value = debit

        processor = self.make_one(
            refund_cls=Refund,
            debit_cls=Debit,
            customer_cls=Customer,
        )
        result = processor.refund(transaction)
        self.assertEqual(result['processor_uri'], 'MOCK_REFUND_URI')
        self.assertEqual(result['status'],
                         self.transaction_model.statuses.SUCCEEDED)

        Debit.fetch.assert_called_once_with(transaction.reference_to.processor_uri)
        description = (
            'Generated by Billy from invoice {}'
            .format(self.invoice.guid)
        )
        expected_kwargs = dict(
            amount=transaction.amount,
            meta={'billy.transaction_guid': transaction.guid},
            description=description,
            appears_on_statement_as='hello baby',
        )
        debit.refund.assert_called_once_with(**expected_kwargs)

    def test_refund_with_created_record(self):
        transaction = self._create_refund_transaction()

        # mock resource
        resource = mock.Mock(
            href='MOCK_BALANCED_REFUND_URI',
            status='succeeded',
        )
        # mock page
        page = mock.Mock()
        page.one.return_value = resource
        # mock debit instance
        debit = mock.Mock()
        debit.refund.return_value = mock.Mock(
            href='MOCK_REFUND_URI',
            status='succeeded',
        )
        debit.refund.__name__ = 'refund'
        # mock customer class
        Customer = mock.Mock()
        Customer.fetch.return_value = mock.Mock()
        # mock refund class
        Refund = mock.Mock()
        Refund.query.filter.return_value = page
        # mock debit class
        Debit = mock.Mock()
        Debit.fetch.return_value = debit

        processor = self.make_one(
            refund_cls=Refund,
            debit_cls=Debit,
            customer_cls=Customer,
        )
        result = processor.refund(transaction)
        self.assertEqual(result['processor_uri'], 'MOCK_BALANCED_REFUND_URI')
        self.assertEqual(result['status'],
                         self.transaction_model.statuses.SUCCEEDED)

        # make sure we won't duplicate refund
        self.assertFalse(debit.refund.called)
        # make sure query is made correctly
        expected_kwargs = {'meta.billy.transaction_guid': transaction.guid}
        Refund.query.filter.assert_called_once_with(**expected_kwargs)

    def test_api_key_is_ensured(self):
        processor = self.make_one(configure_api_key=False)
        for method_name in [
            'callback',
            'register_callback',
            'validate_customer',
            'create_customer',
            'prepare_customer',
            'validate_customer',
            'debit',
            'credit',
            'refund',
        ]:
            with self.assertRaises(AssertionError):
                method = getattr(processor, method_name)
                if method_name in ['callback', 'register_callback']:
                    method(None, None)
                else:
                    method(None)

    def test_status_mapping(self):
        processor = self.make_one(configure_api_key=False)

        def assert_status(api_status, expected_status):
            res = mock.Mock(uri='MOCK_URI', status=api_status)
            result = processor._resource_to_result(res)
            self.assertEqual(result['status'], expected_status)

        assert_status('pending', self.transaction_model.statuses.PENDING)
        assert_status('succeeded', self.transaction_model.statuses.SUCCEEDED)
        assert_status('paid', self.transaction_model.statuses.SUCCEEDED)
        assert_status('failed', self.transaction_model.statuses.FAILED)
        assert_status('reversed', self.transaction_model.statuses.FAILED)

        # default to pending when encounter unknown status
        assert_status('unexpected', self.transaction_model.statuses.PENDING)
        assert_status('xxx', self.transaction_model.statuses.PENDING)
