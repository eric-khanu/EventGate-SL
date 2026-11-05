from django.urls import path

from . import views

app_name = "advertisements"

urlpatterns = [
    path("",                      views.dashboard,       name="dashboard"),
    path("subscribe/",            views.subscribe,       name="subscribe"),
    path("campaigns/new/",        views.campaign_create, name="campaign_create"),
    path("track/impression/<int:pk>/", views.impression, name="impression"),
    path("track/click/<int:pk>/",      views.click,      name="click"),
]