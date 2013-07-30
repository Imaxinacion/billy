from __future__ import unicode_literals

from flask.ext.restful import fields

from utils.intervals import IntervalViewField

plan_sub_view = {
    # Todo: figure out why some arent showing...
    'id': fields.String(attribute='guid'),
    'created_at': fields.DateTime(),
    'plan_id': fields.String(attribute='plan.external_id'),
    'customer_id': fields.Integer(attribute='customer.external_id'),
    'is_active': fields.Boolean(),
    'is_enrolled': fields.Boolean(),
    'invoices': fields.List()
}
