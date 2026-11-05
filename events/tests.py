from django.test import TestCase
from django.contrib.auth import get_user_model
from django.utils import timezone
from datetime import timedelta
from events.models import Event, TicketTier
from orders.models import Order,OrderItem
from tickets.models import Ticket
from tickets.services import validate_and_consume
class TicketScanTests(TestCase):
 def setUp(self):
  U=get_user_model(); self.owner=U.objects.create_user(username='organizer',password='StrongPassword!123'); self.other=U.objects.create_user(username='other',password='StrongPassword!123')
  now=timezone.now(); self.event=Event.objects.create(organizer=self.owner,title='Test Event',summary='Summary',description='Description',venue='Freetown',district='Western Area',region='Western Area',starts_at=now-timedelta(minutes=10),ends_at=now+timedelta(hours=4),contact_phone='+232000000',status='published')
  self.tier=TicketTier.objects.create(event=self.event,name='General',price=100,quantity=10,sold=1)
  order=Order.objects.create(buyer=self.other,email='x@example.com',phone='+232000000',currency='SLE',total=100,payment_method='orange_money',status='paid')
  item=OrderItem.objects.create(order=order,tier=self.tier,quantity=1,unit_price=100,line_total=100); self.ticket,self.raw=Ticket.issue(item,self.other)
 def test_valid_scan_is_consumed_once(self):
  first=validate_and_consume(ticket_id=self.ticket.public_id,token=self.raw,scanner=self.owner)
  second=validate_and_consume(ticket_id=self.ticket.public_id,token=self.raw,scanner=self.owner)
  self.assertTrue(first['ok']); self.assertFalse(second['ok']); self.ticket.refresh_from_db(); self.assertEqual(self.ticket.status,'used')
 def test_wrong_organizer_cannot_scan(self):
  result=validate_and_consume(ticket_id=self.ticket.public_id,token=self.raw,scanner=self.other)
  self.assertFalse(result['ok']); self.assertEqual(result['code'],'FORBIDDEN')
 def test_wrong_token_rejected(self):
  result=validate_and_consume(ticket_id=self.ticket.public_id,token='bad-token',scanner=self.owner)
  self.assertFalse(result['ok']); self.assertEqual(result['code'],'INVALID')
