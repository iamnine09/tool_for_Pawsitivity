from django.urls import path
from . import views

app_name = 'qrgen'

urlpatterns = [
    path('', views.index, name='index'),
    path('download/', views.download_pdf, name='download_pdf'),
]