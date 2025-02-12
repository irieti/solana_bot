"""
URL configuration for solana_bot project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/5.1/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""

from django.contrib import admin
from django.urls import path
from . import views

urlpatterns = [
    path("admin/", admin.site.urls),
    path("", views.index, name="index"),
    path("start-loop/", views.start_loop, name="start_loop"),
    path("stop-loop/", views.stop_loop, name="stop_loop"),
    path("get-active-loops/", views.get_active_loops, name="get_active_loops"),
    path("get-loop-updates/", views.get_loop_updates, name="get-loop-updates"),
    path("new-wallet/", views.new_wallet, name="new-wallet"),
    path("get_wallets/", views.get_wallets, name="get_wallets"),
    path("webhook/", views.webhook, name="webhook"),
    path("add_wallets_excel/", views.add_wallets_excel, name="add_wallets_excel"),
    path("webhook_status/", views.webhook_status, name="webhook_status"),
    path("toggle_webhook/", views.toggle_webhook, name="toggle_webhook"),
]
