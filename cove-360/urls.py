from django.conf.urls import url

from cove import urlpatterns

urlpatterns += [
    url(r'^data/(.+)$', 'cove-360.views.explore_360', name='explore'),
    url(r'^common_errors', 'cove360.views.common_errors', name='common_errors')
]
