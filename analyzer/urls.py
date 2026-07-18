from django.urls import path

from . import views

urlpatterns = [
    path("", views.index, name="index"),
    path("search/", views.search, name="search"),
    path("api/analyze/", views.analyze, name="analyze"),
    path("api/search/", views.search_repos, name="search_repos"),
]
