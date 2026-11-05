from datetime import timedelta
from decimal import Decimal
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from .models import AdPlan, AdSubscription, AdCampaign

class AdvertisingModelTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username='advertiser-test', password='Long-Test-Password-123!')
        self.plan = AdPlan.objects.create(name='Quarterly', duration_months=3, price=Decimal('500'), currency='SLE')
    def test_campaign_ctr_handles_zero_impressions(self):
        campaign = AdCampaign.objects.create(advertiser=self.user, company_name='Example Co', title='Example', target_url='https://example.com')
        self.assertEqual(campaign.ctr, 0)
    def test_activation_sets_expiry_and_verifier(self):
        admin = get_user_model().objects.create_superuser(username='admin-test', email='admin@example.com', password='Long-Admin-Password-123!')
        sub = AdSubscription.objects.create(advertiser=self.user, plan=self.plan, amount=self.plan.price, currency=self.plan.currency, payment_method='bank_transfer', payment_reference='REF-123')
        sub.activate(admin)
        sub.refresh_from_db()
        self.assertEqual(sub.status, AdSubscription.Status.ACTIVE)
        self.assertEqual(sub.verified_by, admin)
        self.assertGreater(sub.expires_at, sub.starts_at + timedelta(days=80))
