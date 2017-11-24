# Copied from Django REST Framework.
"""
Handled exceptions raised by API requests.

In addition Django's built in 403 and 404 exceptions are handled.
(`django.http.Http404` and `django.core.exceptions.PermissionDenied`)
"""
from __future__ import unicode_literals

import functools
import json
import math

from django.core.exceptions import PermissionDenied as dj_PermissionDenied
from django.http.request import QueryDict
from django.http.response import JsonResponse, Http404
from django.utils import six
from django.utils.encoding import force_text
from django.utils.translation import ugettext_lazy as _
from django.utils.translation import ungettext

from .. import http_status as status


def _handle_exception(exc, context=None):
    if isinstance(exc, APIException):
        headers = {}
        if getattr(exc, 'auth_header', None):
            headers['WWW-Authenticate'] = exc.auth_header
        if getattr(exc, 'wait', None):
            headers['Retry-After'] = '%d' % math.ceil(exc.wait)

        if isinstance(exc.detail, ErrorDetail):
            data = {'code': exc.detail.code, 'message': exc.detail}
        else:
            data = {
                'code': 'error_with_detail',
                'message': 'Error with detail.',
                'detail': exc.detail
            }
        return JsonResponse(data, status=exc.status_code, headers=headers)
    elif isinstance(exc, Http404):
        return JsonResponse({
            'code': NotFound.default_code,
            'message': NotFound.default_detail
        }, status=status.HTTP_404_NOT_FOUND)
    elif isinstance(exc, dj_PermissionDenied):
        return JsonResponse({
            'code': PermissionDenied.default_code,
            'message': PermissionDenied.default_detail
        }, status=status.HTTP_403_FORBIDDEN)
    else:
        return JsonResponse({
            'code': APIException.default_code,
            'message': APIException.default_detail
        }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


def ajax_login_required(view_func):
    @functools.wraps(view_func)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated():
            return JsonResponse({
                'code': NotAuthenticated.default_code,
                'message': NotAuthenticated.default_detail
            }, status=status.HTTP_401_UNAUTHORIZED)
        return view_func(request, *args, **kwargs)

    return wrapper


def ajax_staff_member_required(view_func):
    @ajax_login_required
    @functools.wraps(view_func)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_staff:
            return JsonResponse({
                'code': PermissionDenied.default_code,
                'message': PermissionDenied.default_detail
            }, status=status.HTTP_403_FORBIDDEN)
        return view_func(request, *args, **kwargs)

    return wrapper


def api_view(methods=('GET', 'POST'), login_required=False,
             staff_member_required=False):

    def decorator(func):
        @functools.wraps(func)
        def wrapper(request, *args, **kwargs):
            try:
                if request.method not in methods:
                    raise MethodNotAllowed(request.method)

                result = func(request, *args, **kwargs)
                if result and type(result) != dict:
                    return result

                response = {'code': 'ok', 'message': 'Ok.'}
                response.update(result or {})
                status_code = 200
                if response.get('status'):
                    status_code = response.pop('status')
                return JsonResponse(response, status=status_code)
            except Exception as exc:
                return _handle_exception(exc)

        if staff_member_required:
            wrapper = ajax_staff_member_required(wrapper)
        elif login_required:
            wrapper = ajax_login_required(wrapper)
        return wrapper

    return decorator


def api_token_required(validator, token_field='token'):
    """Use together with the api_view decorator."""

    def decorator(view_func):
        @functools.wraps(view_func)
        def wrapper(request, *args, **kwargs):
            if request.method in ('HEAD', 'OPTION'):
                return view_func(request, *args, **kwargs)

            token = None
            if request.method == 'GET':
                token = request.GET.get(token_field)
            elif (request.META['CONTENT_TYPE'] ==
                    'application/x-www-form-urlencoded'):
                if request.method == 'POST':
                    query_dict = request.POST
                else:
                    query_dict = QueryDict(request.body)
                token = query_dict.get(token_field)
                # cache query dict to request for better performance
                request.QUERY_DICT = query_dict
            elif request.META['CONTENT_TYPE'] == 'application/json':
                try:
                    if isinstance(request.body, six.string_types):
                        data = json.loads(request.body)
                    else:
                        data = json.loads(request.body.decode('utf-8'))
                    token = data.get(token_field, None)
                    # cache json data to request for better performance
                    request.JSON = data
                except Exception as err:
                    raise ParseError('Invalid JSON data.')
            else:
                # how can we arrive here?
                pass

            if not token:
                raise NotAuthenticated()
            if not validator(request, token):
                raise AuthenticationFailed()

            return view_func(request, *args, **kwargs)

        return wrapper

    return decorator


def _get_error_details(data, default_code=None):
    """
    Descend into a nested data structure, forcing any
    lazy translation strings or strings into `ErrorDetail`.
    """
    if isinstance(data, list):
        ret = [
            _get_error_details(item, default_code) for item in data
        ]
        return ret
    elif isinstance(data, dict):
        ret = {
            key: _get_error_details(value, default_code)
            for key, value in data.items()
        }
        return ret

    text = force_text(data)
    code = getattr(data, 'code', default_code)
    return ErrorDetail(text, code)


def _get_codes(detail):
    if isinstance(detail, list):
        return [_get_codes(item) for item in detail]
    elif isinstance(detail, dict):
        return {key: _get_codes(value) for key, value in detail.items()}
    return detail.code


def _get_full_details(detail):
    if isinstance(detail, list):
        return [_get_full_details(item) for item in detail]
    elif isinstance(detail, dict):
        return {key: _get_full_details(value) for key, value in detail.items()}
    return {
        'message': detail,
        'code': detail.code
    }


class ErrorDetail(six.text_type):
    """
    A string-like object that can additionally have a code.
    """
    code = None

    def __new__(cls, string, code=None):
        self = super(ErrorDetail, cls).__new__(cls, string)
        self.code = code
        return self


class APIException(Exception):
    """
    Base class for REST framework exceptions.
    Subclasses should provide `.status_code` and `.default_detail` properties.
    """
    status_code = status.HTTP_500_INTERNAL_SERVER_ERROR
    default_detail = _('A server error occurred.')
    default_code = 'error'

    def __init__(self, detail=None, code=None):
        if detail is None:
            detail = self.default_detail
        if code is None:
            code = self.default_code

        self.detail = _get_error_details(detail, code)

    def __str__(self):
        return six.text_type(self.detail)

    def get_codes(self):
        """
        Return only the code part of the error details.

        Eg. {"name": ["required"]}
        """
        return _get_codes(self.detail)

    def get_full_details(self):
        """
        Return both the message & code parts of the error details.

        Eg. {"name": [{"message": "This field is required.", "code": "required"}]}
        """
        return _get_full_details(self.detail)


# The recommended style for using `ValidationError` is to keep it namespaced
# under `api`, in order to minimize potential confusion with Django's
# built in `ValidationError`. For example:
#
# import utils.django.api as api_util
# raise api_util.ValidationError('Value was invalid')

class ValidationError(APIException):
    status_code = status.HTTP_400_BAD_REQUEST
    default_detail = _('Invalid input.')
    default_code = 'invalid'

    def __init__(self, detail=None, code=None):
        if detail is None:
            detail = self.default_detail
        if code is None:
            code = self.default_code

        # For validation failures, we may collect many errors together,
        # so the details should always be coerced to a list if not already.
        if not isinstance(detail, dict) and not isinstance(detail, list):
            detail = [detail]

        self.detail = _get_error_details(detail, code)


class ParseError(APIException):
    status_code = status.HTTP_400_BAD_REQUEST
    default_detail = _('Malformed request.')
    default_code = 'parse_error'


class AuthenticationFailed(APIException):
    status_code = status.HTTP_401_UNAUTHORIZED
    default_detail = _('Incorrect authentication credentials.')
    default_code = 'authentication_failed'


class NotAuthenticated(APIException):
    status_code = status.HTTP_401_UNAUTHORIZED
    default_detail = _('Authentication credentials were not provided.')
    default_code = 'not_authenticated'


class PermissionDenied(APIException):
    status_code = status.HTTP_403_FORBIDDEN
    default_detail = _('You do not have permission to perform this action.')
    default_code = 'permission_denied'


class NotFound(APIException):
    status_code = status.HTTP_404_NOT_FOUND
    default_detail = _('Not found.')
    default_code = 'not_found'


class MethodNotAllowed(APIException):
    status_code = status.HTTP_405_METHOD_NOT_ALLOWED
    default_detail = _('Method "{method}" not allowed.')
    default_code = 'method_not_allowed'

    def __init__(self, method, detail=None, code=None):
        if detail is None:
            detail = force_text(self.default_detail).format(method=method)
        super(MethodNotAllowed, self).__init__(detail, code)


class NotAcceptable(APIException):
    status_code = status.HTTP_406_NOT_ACCEPTABLE
    default_detail = _('Could not satisfy the request Accept header.')
    default_code = 'not_acceptable'

    def __init__(self, detail=None, code=None, available_renderers=None):
        self.available_renderers = available_renderers
        super(NotAcceptable, self).__init__(detail, code)


class UnsupportedMediaType(APIException):
    status_code = status.HTTP_415_UNSUPPORTED_MEDIA_TYPE
    default_detail = _('Unsupported media type "{media_type}" in request.')
    default_code = 'unsupported_media_type'

    def __init__(self, media_type, detail=None, code=None):
        if detail is None:
            detail = force_text(self.default_detail).format(media_type=media_type)
        super(UnsupportedMediaType, self).__init__(detail, code)


class Throttled(APIException):
    status_code = status.HTTP_429_TOO_MANY_REQUESTS
    default_detail = _('Request was throttled.')
    extra_detail_singular = 'Expected available in {wait} second.'
    extra_detail_plural = 'Expected available in {wait} seconds.'
    default_code = 'throttled'

    def __init__(self, wait=None, detail=None, code=None):
        if detail is None:
            detail = force_text(self.default_detail)
        if wait is not None:
            wait = math.ceil(wait)
            detail = ' '.join((
                detail,
                force_text(ungettext(self.extra_detail_singular.format(wait=wait),
                                     self.extra_detail_plural.format(wait=wait),
                                     wait))))
        self.wait = wait
        super(Throttled, self).__init__(detail, code)
