# coding: utf-8

import json
import logging
import urlparse

from odoo import api, fields, models, _
from odoo.addons.payment.models.payment_acquirer import ValidationError
from odoo.addons.payment_alipay.controllers.main import AlipayController
from odoo.tools.float_utils import float_compare


_logger = logging.getLogger(__name__)


class AcquirerAlipay(models.Model):
    _inherit = 'payment.acquirer'

    provider = fields.Selection(selection_add=[('alipay', 'Alipay')])
    alipay_email_account = fields.Char('Alipay Email ID', required_if_provider='alipay', groups='base.group_user')
    alipay_seller_account = fields.Char(
        'Alipay Merchant ID', groups='base.group_user',
        help='The Merchant ID is used to ensure communications coming from Alipay are valid and secured.')
    alipay_use_ipn = fields.Boolean('Use IPN', default=True, help='Alipay Instant Payment Notification', groups='base.group_user')
    # Server 2 server
    alipay_api_enabled = fields.Boolean('Use Rest API', default=False)
    alipay_api_username = fields.Char('Rest API Username', groups='base.group_user')
    alipay_api_password = fields.Char('Rest API Password', groups='base.group_user')
    alipay_api_access_token = fields.Char('Access Token', groups='base.group_user')
    alipay_api_access_token_validity = fields.Datetime('Access Token Validity', groups='base.group_user')
    # Default alipay fees
    fees_dom_fixed = fields.Float(default=0.35)
    fees_dom_var = fields.Float(default=3.4)
    fees_int_fixed = fields.Float(default=0.35)
    fees_int_var = fields.Float(default=3.9)

    def _get_feature_support(self):
        """Get advanced feature support by provider.

        Each provider should add its technical in the corresponding
        key for the following features:
            * fees: support payment fees computations
            * authorize: support authorizing payment (separates
                         authorization and capture)
            * tokenize: support saving payment data in a payment.tokenize
                        object
        """
        res = super(AcquirerAlipay, self)._get_feature_support()
        res['fees'].append('alipay')
        return res

    @api.model
    def _get_alipay_urls(self, environment):
        """ Alipay URLS """
        if environment == 'prod':
            return {
                'alipay_form_url': 'https://www.alipay.com/cgi-bin/webscr',
                'alipay_rest_url': 'https://api.alipay.com/v1/oauth2/token',
            }
        else:
            return {
                'alipay_form_url': 'https://www.sandbox.alipay.com/cgi-bin/webscr',
                'alipay_rest_url': 'https://api.sandbox.alipay.com/v1/oauth2/token',
            }

    @api.multi
    def alipay_compute_fees(self, amount, currency_id, country_id):
        """ Compute alipay fees.

            :param float amount: the amount to pay
            :param integer country_id: an ID of a res.country, or None. This is
                                       the customer's country, to be compared to
                                       the acquirer company country.
            :return float fees: computed fees
        """
        if not self.fees_active:
            return 0.0
        country = self.env['res.country'].browse(country_id)
        if country and self.company_id.country_id.id == country.id:
            percentage = self.fees_dom_var
            fixed = self.fees_dom_fixed
        else:
            percentage = self.fees_int_var
            fixed = self.fees_int_fixed
        fees = (percentage / 100.0 * amount + fixed) / (1 - percentage / 100.0)
        return fees

    @api.multi
    def alipay_form_generate_values(self, values):
        base_url = self.env['ir.config_parameter'].sudo().get_param('web.base.url')

        alipay_tx_values = dict(values)
        alipay_tx_values.update({
            'cmd': '_xclick',
            'business': self.alipay_email_account,
            'item_name': '%s: %s' % (self.company_id.name, values['reference']),
            'item_number': values['reference'],
            'amount': values['amount'],
            'currency_code': values['currency'] and values['currency'].name or '',
            'address1': values.get('partner_address'),
            'city': values.get('partner_city'),
            'country': values.get('partner_country') and values.get('partner_country').code or '',
            'state': values.get('partner_state') and (values.get('partner_state').code or values.get('partner_state').name) or '',
            'email': values.get('partner_email'),
            'zip_code': values.get('partner_zip'),
            'first_name': values.get('partner_first_name'),
            'last_name': values.get('partner_last_name'),
            'alipay_return': '%s' % urlparse.urljoin(base_url, AlipayController._return_url),
            'notify_url': '%s' % urlparse.urljoin(base_url, AlipayController._notify_url),
            'cancel_return': '%s' % urlparse.urljoin(base_url, AlipayController._cancel_url),
            'handling': '%.2f' % alipay_tx_values.pop('fees', 0.0) if self.fees_active else False,
            'custom': json.dumps({'return_url': '%s' % alipay_tx_values.pop('return_url')}) if alipay_tx_values.get('return_url') else False,
        })
        return alipay_tx_values

    @api.multi
    def alipay_get_form_action_url(self):
        return self._get_alipay_urls(self.environment)['alipay_form_url']


class TxAlipay(models.Model):
    _inherit = 'payment.transaction'

    alipay_txn_type = fields.Char('Transaction type')

    # --------------------------------------------------
    # FORM RELATED METHODS
    # --------------------------------------------------

    @api.model
    def _alipay_form_get_tx_from_data(self, data):
        reference, txn_id = data.get('item_number'), data.get('txn_id')
        if not reference or not txn_id:
            error_msg = _('Alipay: received data with missing reference (%s) or txn_id (%s)') % (reference, txn_id)
            _logger.info(error_msg)
            raise ValidationError(error_msg)

        # find tx -> @TDENOTE use txn_id ?
        txs = self.env['payment.transaction'].search([('reference', '=', reference)])
        if not txs or len(txs) > 1:
            error_msg = 'Alipay: received data for reference %s' % (reference)
            if not txs:
                error_msg += '; no order found'
            else:
                error_msg += '; multiple order found'
            _logger.info(error_msg)
            raise ValidationError(error_msg)
        return txs[0]

    @api.multi
    def _alipay_form_get_invalid_parameters(self, data):
        invalid_parameters = []
        _logger.info('Received a notification from Alipay with IPN version %s', data.get('notify_version'))
        if data.get('test_ipn'):
            _logger.warning(
                'Received a notification from Alipay using sandbox'
            ),

        # TODO: txn_id: shoudl be false at draft, set afterwards, and verified with txn details
        if self.acquirer_reference and data.get('txn_id') != self.acquirer_reference:
            invalid_parameters.append(('txn_id', data.get('txn_id'), self.acquirer_reference))
        # check what is buyed
        if float_compare(float(data.get('mc_gross', '0.0')), (self.amount + self.fees), 2) != 0:
            invalid_parameters.append(('mc_gross', data.get('mc_gross'), '%.2f' % self.amount))  # mc_gross is amount + fees
        if data.get('mc_currency') != self.currency_id.name:
            invalid_parameters.append(('mc_currency', data.get('mc_currency'), self.currency_id.name))
        if 'handling_amount' in data and float_compare(float(data.get('handling_amount')), self.fees, 2) != 0:
            invalid_parameters.append(('handling_amount', data.get('handling_amount'), self.fees))
        # check buyer
        if self.payment_token_id and data.get('payer_id') != self.payment_token_id.acquirer_ref:
            invalid_parameters.append(('payer_id', data.get('payer_id'), self.payment_token_id.acquirer_ref))
        # check seller
        if data.get('receiver_id') and self.acquirer_id.alipay_seller_account and data['receiver_id'] != self.acquirer_id.alipay_seller_account:
            invalid_parameters.append(('receiver_id', data.get('receiver_id'), self.acquirer_id.alipay_seller_account))
        if not data.get('receiver_id') or not self.acquirer_id.alipay_seller_account:
            # Check receiver_email only if receiver_id was not checked.
            # In Alipay, this is possible to configure as receiver_email a different email than the business email (the login email)
            # In Odoo, there is only one field for the Alipay email: the business email. This isn't possible to set a receiver_email
            # different than the business email. Therefore, if you want such a configuration in your Alipay, you are then obliged to fill
            # the Merchant ID in the Alipay payment acquirer in Odoo, so the check is performed on this variable instead of the receiver_email.
            # At least one of the two checks must be done, to avoid fraudsters.
            if data.get('receiver_email') != self.acquirer_id.alipay_email_account:
                invalid_parameters.append(('receiver_email', data.get('receiver_email'), self.acquirer_id.alipay_email_account))

        return invalid_parameters

    @api.multi
    def _alipay_form_validate(self, data):
        status = data.get('payment_status')
        res = {
            'acquirer_reference': data.get('txn_id'),
            'alipay_txn_type': data.get('payment_type'),
        }
        if status in ['Completed', 'Processed']:
            _logger.info('Validated Alipay payment for tx %s: set as done' % (self.reference))
            res.update(state='done', date_validate=data.get('payment_date', fields.datetime.now()))
            return self.write(res)
        elif status in ['Pending', 'Expired']:
            _logger.info('Received notification for Alipay payment %s: set as pending' % (self.reference))
            res.update(state='pending', state_message=data.get('pending_reason', ''))
            return self.write(res)
        else:
            error = 'Received unrecognized status for Alipay payment %s: %s, set as error' % (self.reference, status)
            _logger.info(error)
            res.update(state='error', state_message=error)
            return self.write(res)
