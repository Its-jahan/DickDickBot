"""Regression tests for paid fulfilment and per-group copy mode.

Run against a disposable PostgreSQL database via DATABASE_URL; never point this file at
production because setUpClass initializes the complete application schema.
"""
import os
import unittest
from unittest.mock import patch
from uuid import uuid4

import bot
import db
import webapp


@unittest.skipUnless(os.environ.get('DATABASE_URL'),
                     'DATABASE_URL must point to a disposable test database')
class StarsAndToneTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        db.init_db()

    def setUp(self):
        self.user_id = int(str(uuid4().int)[:12])
        self.chat_id = -int(str(uuid4().int)[:12])
        db.track_chat(self.chat_id, 'Test league')
        db.get_user(self.user_id, self.chat_id, 'tester', 'Test Player')

    def order(self, sku='size_50'):
        order_id, product = bot.create_star_order(self.user_id, self.chat_id, sku)
        self.assertIsNotNone(order_id)
        return order_id, product

    def test_tone_defaults_and_switches_per_group(self):
        self.assertEqual(db.get_chat_tone(self.chat_id), 'adult')
        self.assertTrue(db.set_chat_tone(self.chat_id, 'polite'))
        self.assertEqual(db.get_chat_tone(self.chat_id), 'polite')
        self.assertEqual(db.get_chat_tones([self.chat_id])[self.chat_id], 'polite')
        self.assertIn('قدرت', bot.polite_text('دودول و کیر'))
        self.assertNotIn('دودول', bot.polite_text('دودول و کیر'))

    def test_size_payment_is_validated_and_fulfilled_once(self):
        order_id, product = self.order('size_50')
        charge_id = 'charge-size-' + str(uuid4())
        self.assertFalse(db.claim_star_checkout(order_id, self.user_id, 'USD', product['stars']))
        self.assertFalse(db.claim_star_checkout(order_id, self.user_id + 1, 'XTR', product['stars']))
        self.assertFalse(db.claim_star_checkout(order_id, self.user_id, 'XTR', product['stars'] + 1))
        self.assertTrue(db.claim_star_checkout(order_id, self.user_id, 'XTR', product['stars']))

        paid = db.fulfill_star_order(order_id, self.user_id, 'XTR', product['stars'], charge_id)
        self.assertFalse(paid['duplicate'])
        self.assertEqual(paid['quantity'], 50)
        size, _last, _perk = db.get_user(self.user_id, self.chat_id, None, None)
        self.assertEqual(size, 50)

        replay = db.fulfill_star_order(order_id, self.user_id, 'XTR', product['stars'], charge_id)
        self.assertTrue(replay['duplicate'])
        size, _last, _perk = db.get_user(self.user_id, self.chat_id, None, None)
        self.assertEqual(size, 50)
        self.assertIsNone(db.fulfill_star_order(
            order_id, self.user_id, 'XTR', product['stars'], 'different-charge'))

        with db.get_connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT count(*) FROM size_log WHERE chat_id=%s AND user_id=%s "
                        "AND source='telegram_stars'", (self.chat_id, self.user_id))
            self.assertEqual(cur.fetchone()[0], 1)

    def test_item_payment_adds_one_inventory_item(self):
        order_id, product = self.order('item_01')
        charge_id = 'charge-item-' + str(uuid4())
        self.assertTrue(db.claim_star_checkout(order_id, self.user_id, 'XTR', product['stars']))
        first = db.fulfill_star_order(order_id, self.user_id, 'XTR', product['stars'], charge_id)
        second = db.fulfill_star_order(order_id, self.user_id, 'XTR', product['stars'], charge_id)
        self.assertFalse(first['duplicate'])
        self.assertTrue(second['duplicate'])
        self.assertEqual(dict(db.get_inventory(self.user_id, self.chat_id))[product['item']], 1)

    def test_mini_app_invoice_and_admin_tone_routes(self):
        identity = (self.user_id, 'Test Player', 'tester')
        client = webapp.app.test_client()
        with patch.object(webapp, '_auth', return_value=identity), \
                patch.object(webapp, '_tg_api', return_value='https://t.me/$testinvoice'):
            response = client.post('/api/stars/invoice',
                                   json={'chat_id': self.chat_id, 'sku': 'size_50'})
            self.assertEqual(response.status_code, 200)
            payload = response.get_json()
            self.assertTrue(payload['ok'])
            self.assertTrue(db.get_star_order(payload['order_id'], self.user_id))

        with patch.object(webapp, '_auth', return_value=identity), \
                patch.object(webapp, '_tg_is_admin', return_value=False):
            denied = client.post('/api/settings/tone',
                                 json={'chat_id': self.chat_id, 'mode': 'polite'})
            self.assertEqual(denied.status_code, 403)
        with patch.object(webapp, '_auth', return_value=identity), \
                patch.object(webapp, '_tg_is_admin', return_value=True):
            changed = client.post('/api/settings/tone',
                                  json={'chat_id': self.chat_id, 'mode': 'polite'})
            self.assertEqual(changed.status_code, 200)
            self.assertEqual(db.get_chat_tone(self.chat_id), 'polite')


if __name__ == '__main__':
    unittest.main()
