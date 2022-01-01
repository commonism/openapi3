import datetime
import pathlib
from typing import Any, List, Optional, Dict, Union, Callable

from pydantic import Field
import httpx
import yarl

from .components import Components
from .errors import ReferenceResolutionError, SpecError
from .general import Reference, JSONPointer, JSONReference
from .info import Info
from .object_base import ObjectExtended, ObjectBase
from .paths import PathItem, SecurityRequirement, _validate_parameters, Operation
from .servers import Server
from .schemas import Schema, Discriminator
from .tag import Tag
from .request import Request, AsyncRequest
from .loader import Loader

HTTP_METHODS = frozenset(["get","delete","head","post","put","patch","trace"])

class OpenAPI:

    @property
    def paths(self):
        return self._spec.paths

    @property
    def components(self):
        return self._spec.components

    @property
    def info(self):
        return self._spec.info

    @property
    def openapi(self):
        return self._spec.openapi

    @property
    def servers(self):
        return self._spec.servers

    def __init__(
            self,
            raw_document,
            validate=False,
            ssl_verify=None,
            use_session=False,
            session_factory=requests.Session,
            loader=None):
        """
        Creates a new OpenAPI document from a loaded spec file.  This is
        overridden here because we need to specify the path in the parent
        class' constructor.

        :param raw_document: The raw OpenAPI file loaded into python
        :type raw_document: dct
        :param validate: If True, don't fail on errors, but instead capture all
                         errors, continuing along the spec as best as possible,
                         and make them available when parsing is complete.
        :type validate: bool
        :param ssl_verify: Decide if to use ssl verification to the requests or not,
                           in case an str is passed, will be used as the CA.
        :type ssl_verify: bool, str, None
        :param use_session: Should we use a consistant session between API calls
        :type use_session: bool
        """

        self.loader = loader

        self._validation_mode = validate
        self._spec_error = None
        self._operation_map = dict()
        self._security = None
        self._cached = dict()
        self._ssl_verify = ssl_verify

        self._session = None
        if use_session:
            self._session = session_factory()


        try:
            self._spec = OpenAPISpec.parse_obj(raw_document)
        except Exception as e:
            if not self._validation_mode:
                raise
            self._spec_error = e
            return

        try:
            self._spec._resolve_references(self)
        except ValueError as e:
            if not self._validation_mode:
                raise
            self._spec_error = e
            return

        for name, schema in self.components.schemas.items():
            schema._identity = name

        for path,obj in self.paths.items():
            for m in obj.__fields_set__ & HTTP_METHODS:
                op = getattr(obj, m)
                _validate_parameters(op, path)
                if op.operationId is None:
                    continue
                formatted_operation_id = op.operationId.replace(" ", "_")
                self._register_operation(formatted_operation_id, (m, path, op))
                for r, response in op.responses.items():
                    if isinstance(response, Reference):
                        continue
                    for c, content in response.content.items():
                        if content.schema_ is None:
                            continue
                        if isinstance(content.schema_, Schema):
                            content.schema_._identity = f"{path}.{m}.{r}.{c}"

    # public methods
    def authenticate(self, security_scheme, value):
        """
        Authenticates all subsequent requests with the given arguments.

        TODO - this should support more than just HTTP Auth
        """

        # authentication is optional and can be disabled
        if security_scheme is None:
            self._security = None
            return

        if security_scheme not in self._spec.components.securitySchemes:
            raise ValueError('{} does not accept security scheme {}'.format(
                self.info.title, security_scheme))

        self._security = {security_scheme: value}


    def errors(self):
        """
        In Validation Mode, returns all errors encountered from parsing a spec.
        This should not be called if not in Validation Mode.

        :returns: The errors encountered during the parsing of this spec.
        :rtype: ValidationError
        """
        if not self._validation_mode:
            raise RuntimeError('This client is not in Validation Mode, cannot '
                               'return errors!')
        return self._spec_error


    # private methods
    def _register_operation(self, operation_id, opInfo):
        """
        Adds an Operation to this spec's _operation_map, raising an error if the
        OperationId has already been registered.

        :param operation_id: The operation ID to register
        :type operation_id: str
        :param opInfo: The operation to register
        :type opInfo: Operation
        """
        if operation_id in self._operation_map:
            raise SpecError(f"Duplicate operationId {operation_id}", element=opInfo)
        self._operation_map[operation_id] = opInfo

    def _get_callable(self, method, path, request:Operation):
        """
        A helper function to create OperationCallable objects for __getattribute__,
        pre-initialized with the required values from this object.

        :param request: The Operation the callable should call
        :type request: callable (Operation.request)

        :returns: The callable that executes this operation with this object's
                  configuration.
        :rtype: Request
        """
        return Request(self, method, path, request)

    def __getattr__(self, attr):
        """
        Extended __getattribute__ function to allow resolving dynamic function
        names.  The purpose of this is to call syntax like this::

           spec = OpenAPI(raw_spec)
           spec.call_operationId()

        This method will intercept the dot notation above (spec.call_operationId)
        and look up the requested operation, returning a callable object that
        will then immediately be called by the parenthesis.

        :param attr: The attribute we're retrieving
        :type attr: str

        :returns: The attribute requested
        :rtype: any
        :raises AttributeError: if the requested attribute does not exist
        """
        if attr.startswith('call_'):
            _, operationId = attr.split('_', 1)
            if operationId not in self._operation_map:
                raise AttributeError('{} has no operation {}'.format(
                    self.info.title, operationId))
            method, path, op = self._operation_map[operationId]
            return self._get_callable(method, path, op)
        raise KeyError(attr)

    def _load(self, i):
        data = self.loader.load(i)
        return OpenAPISpec.parse_obj(data)


    @property
    def _(self):
        return OperationIndex(self)


    def resolve_jr(self, root: "OpenAPISpec", obj, value: Reference):
        url,jp = JSONReference.split(value.ref)
        if url != '':
            url = pathlib.Path(url)
            if url not in self._cached:
                self._cached[url] = self._load(url)
            root = self._cached[url]

        try:
            return root.resolve_jp(jp)
        except ReferenceResolutionError as e:
            # add metadata to the error
            e.element = obj
            raise


class OpenAPISpec(ObjectExtended):
    """
    This class represents the root of the OpenAPI schema document, as defined
    in `the spec`_

    .. _the spec: https://github.com/OAI/OpenAPI-Specification/blob/main/versions/3.0.3.md#openapi-object
    """

    openapi: str = Field(...)
    info: Info = Field(...)
    servers: Optional[List[Server]] = Field(default=None)
    paths: Dict[str, PathItem] = Field(required=True, default_factory=dict)
    components: Optional[Components] = Field(default_factory=Components)
    security: Optional[List[SecurityRequirement]] = Field(default=None)
    tags: Optional[List[Tag]] = Field(default=None)
    externalDocs: Optional[Dict[Any, Any]] = Field(default_factory=dict)

    class Config:
        underscore_attrs_are_private = True
        arbitrary_types_allowed = True

    def _resolve_references(self, api):
        """
        Resolves all reference objects below this object and notes their original
        value was a reference.
        """
        # don't circular import

        root = self

        def resolve(obj):
            if isinstance(obj, ObjectBase):
                for slot in filter(lambda x: not x.startswith("_"), obj.__fields_set__):
                    value = getattr(obj, slot)
                    if value is None:
                        continue

                    if isinstance(obj, PathItem) and slot == "ref":
                        ref = Reference.construct(ref=value)
                        ref._target = api.resolve_jr(root, obj, ref)
                        setattr(obj, slot, ref)

#                    if isinstance(obj, Discriminator) and slot == "mapping":
#                        mapping = dict()
#                        for k,v in value.items():
#                            mapping[k] = Reference.construct(ref=v)
#                        setattr(obj, slot, mapping)

                    value = getattr(obj, slot)
                    if isinstance(value, Reference):
                        value._target = api.resolve_jr(root, obj, value)
#                        setattr(obj, slot, resolved_value)
                    elif issubclass(type(value), ObjectBase):
                        # otherwise, continue resolving down the tree
                        resolve(value)
                    elif isinstance(value, dict):  # pydantic does not use Map
                        resolve(value)
                    elif isinstance(value, list):
                        # if it's a list, resolve its item's references
                        for item in value:
                            if isinstance(item, Reference):
                                item._target = api.resolve_jr(root, obj, item)
                            elif isinstance(item, (ObjectBase, dict, list)):
                                resolve(item)
                    elif isinstance(value, (str, int, float, datetime.datetime)):
                        continue
                    else:
                        raise TypeError(type(value))
            elif isinstance(obj, dict):
                for k, v in obj.items():
                    if isinstance(v, Reference):
                        if v.ref:
                            v._target = api.resolve_jr(root, obj, v)
                    elif isinstance(v, (ObjectBase, dict, list)):
                        resolve(v)

        resolve(self)


    def resolve_jp(self, jp):
        """
        Given a $ref path, follows the document tree and returns the given attribute.

        :param jp: The path down the spec tree to follow
        :type jp: str #/foo/bar

        :returns: The node requested
        :rtype: ObjectBase
        :raises ValueError: if the given path is not valid
        """
        path = jp.split("/")[1:]
        node = self

        for part in path:
            part = JSONPointer.decode(part)
            if isinstance(node, dict):
                if part not in node:  # pylint: disable=unsupported-membership-test
                    raise ReferenceResolutionError(f'Invalid path {path} in Reference')
                node = node.get(part)
            else:
                if not hasattr(node, part):
                    raise ReferenceResolutionError(f'Invalid path {path} in Reference')
                node = getattr(node, part)

        return node


class OperationIndex:
    def __init__(self, api):
        self._api = api
        self._spec = api._spec

    def __getattr__(self, item):
        pi: PathItem
        for path,pi in self._spec.paths.items():
            op: Operation
            for method in pi.__fields_set__ & HTTP_METHODS:
                op = getattr(pi, method)
                if op.operationId != item:
                    continue
                return Request(self._api, method, path, op)
        raise ValueError(item)



OpenAPISpec.update_forward_refs()