import calendar
import collections
import copy
import datetime
import inspect
import json
import time
import urlparse

import pprint

pp = pprint.PrettyPrinter(indent=4)

from rest_framework import (viewsets, serializers, status, 
        fields, relations, pagination, mixins, throttling)
from rest_framework.exceptions import (ParseError, NotFound, APIException)
from rest_framework.response import Response
from rest_framework.reverse import reverse

import rest_framework_filters as filters

from esmond.api.models import (PSMetadata, PSPointToPointSubject, PSEventTypes, 
    PSMetadataParameters, PSNetworkElementSubject)

from esmond.api.api_v2 import DataObject

#
# Base /archive/ endpoint
#

class ArchiveDataObject(DataObject):
    pass

class ArchiveSerializer(serializers.ModelSerializer):
    class Meta:
        model = PSMetadata
        fields = ('metadata_key', 'subject_type')

    def to_representation(self, obj):
        """
        Modify outgoing data: massage underscore => dash
        """
        ret = super(ArchiveSerializer, self).to_representation(obj)
        for i in ret.keys():
            ret[i.replace('_', '-')] = ret.pop(i)
        return ret

    def to_internal_value(self, data):
        """
        Modify incoming json: massage dash => underscore before calling 
        base code.
        """
        for i in data.keys():
            data[i.replace('-', '_')] = data.pop(i)
        ret = super(ArchiveSerializer, self).to_internal_value(data)

class ArchiveViewset(mixins.CreateModelMixin,
                    mixins.ListModelMixin,
                    mixins.RetrieveModelMixin,
                    viewsets.GenericViewSet):

    """Implements GET and POST model operations w/specific mixins rather 
    than using viewsets.ModelSerializer."""

    serializer_class = ArchiveSerializer
    lookup_field = 'metadata_key'

    def get_queryset(self):
        # Modify for custom filtering logic, etc
        ret = PSMetadata.objects.all()
        return ret

    def list(self, request):
        """Stub for list GET"""
        return super(ArchiveViewset, self).list(request)

    def retrieve(self, request, **kwargs):
        """Stub for detail GET
        'metadata_key', will be one of the kwargs since that is 
        defined as the lookup field for the detail view"""
        return super(ArchiveViewset, self).retrieve(request, **kwargs)

    def create(self, request):
        """Stub for POST object creation"""
        return super(ArchiveViewset, self).create(request)



