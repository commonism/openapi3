from typing import List, Union, cast
import json

import httpx
import pydantic
import pydantic.json

from ..base import SchemaBase, ParameterBase
from ..request import RequestBase, AsyncRequestBase
from ..errors import HTTPStatusError, ContentTypeError, ResponseDecodingError, ResponseSchemaError


class Request(RequestBase):
    """
    This class is returned by instances of the OpenAPI class when members
    formatted like call_operationId are accessed, and a valid Operation is
    found, and allows calling the operation directly from the OpenAPI object
    with the configured values included.  This class is not intended to be used
    directly.
    """

    @property
    def security(self):
        return self.api._security

    @property
    def data(self) -> SchemaBase:
        """the body Schema"""
        return self.operation.requestBody.content["application/json"].schema_

    @property
    def parameters(self) -> List[ParameterBase]:
        """the parameters"""
        return self.operation.parameters + self.root.paths[self.path].parameters

    def args(self, content_type: str = "application/json"):
        op = self.operation
        parameters = op.parameters + self.root.paths[self.path].parameters
        schema = op.requestBody.content[content_type].schema_
        return {"parameters": parameters, "data": schema}

    def return_value(self, http_status: int = 200, content_type: str = "application/json") -> SchemaBase:
        return self.operation.responses[str(http_status)].content[content_type].schema_

    def _prepare_security(self):
        security = self.operation.security or self.api._root.security

        if not security:
            return

        if not self.security:
            if any([{} == i.__root__ for i in security]):
                return
            else:
                options = " or ".join(
                    sorted(map(lambda x: f"{{{x}}}", [" and ".join(sorted(i.__root__.keys())) for i in security]))
                )
                raise ValueError(f"No security requirement satisfied (accepts {options})")

        for s in security:
            if frozenset(s.__root__.keys()) - frozenset(self.security.keys()):
                continue
            for scheme, _ in s.__root__.items():
                value = self.security[scheme]
                self._prepare_secschemes(scheme, value)
            break
        else:
            options = " or ".join(
                sorted(map(lambda x: f"{{{x}}}", [" and ".join(sorted(i.__root__.keys())) for i in security]))
            )
            raise ValueError(
                f"No security requirement satisfied (accepts {options} given {{{' and '.join(sorted(self.security.keys()))}}}"
            )

    def _prepare_secschemes(self, scheme: str, value: Union[str, List[str]]):
        ss = self.root.components.securitySchemes[scheme]

        if ss.type == "http" and ss.scheme_ == "basic":
            self.req.auth = httpx.BasicAuth(*value)

        if ss.type == "http" and ss.scheme_ == "digest":
            self.req.auth = httpx.DigestAuth(*value)

        value = cast(str, value)
        if ss.type == "http" and ss.scheme_ == "bearer":
            header = ss.bearerFormat or "Bearer {}"
            self.req.headers["Authorization"] = header.format(value)

        if ss.type == "mutualTLS":
            # TLS Client certificates (mutualTLS)
            self.req.cert = value

        if ss.type == "apiKey":
            if ss.in_ == "query":
                # apiKey in query parameter
                self.req.params[ss.name] = value

            if ss.in_ == "header":
                # apiKey in query header data
                self.req.headers[ss.name] = value

            if ss.in_ == "cookie":
                self.req.cookies = {ss.name: value}

    def _prepare_parameters(self, provided):
        """
        assigns the parameters provided to the header/path/cookie …

        FIXME: handle parameter location
          https://spec.openapis.org/oas/v3.0.3#parameter-object
          A unique parameter is defined by a combination of a name and location.
        """
        provided = provided or dict()
        possible = {_.name: _ for _ in self.operation.parameters + self.root.paths[self.path].parameters}

        parameters = {
            i.name: i.schema_.default for i in filter(lambda x: x.schema_.default is not None, possible.values())
        }
        parameters.update(provided)

        available = frozenset(parameters.keys())
        accepted = frozenset(possible.keys())
        required = frozenset(map(lambda x: x[0], filter(lambda y: y[1].required, possible.items())))
        if available - accepted:
            raise ValueError(f"Parameter {sorted(available - accepted)} unknown (accepted {sorted(accepted)})")
        if required - available:
            raise ValueError(
                f"Required Parameter {sorted(required - available)} missing (provided {sorted(available)})"
            )

        path_parameters = {}

        for name, value in parameters.items():
            spec = possible[name]
            values = spec._encode(name, value)
            assert isinstance(values, dict)
            if spec.in_ == "path":
                # The string method `format` is incapable of partial updates,
                # as such we need to collect all the path parameters before
                # applying them to the format string.
                path_parameters.update(values)

            if spec.in_ == "query":
                self.req.params.update(values)

            if spec.in_ == "header":
                self.req.headers.update(values)

            if spec.in_ == "cookie":
                self.req.cookies.update(values)

        self.req.url = self.req.url.format(**path_parameters)

    def _prepare_body(self, data):
        if not self.operation.requestBody:
            return

        if data is None and self.operation.requestBody.required:
            raise ValueError("Request Body is required but none was provided.")

        if "application/json" in self.operation.requestBody.content:
            if isinstance(data, (dict, list)):
                pass
            elif isinstance(data, pydantic.BaseModel):
                data = dict(data._iter(to_dict=True))
            else:
                raise TypeError(data)
            data = self.api.plugins.message.marshalled(
                operationId=self.operation.operationId, marshalled=data
            ).marshalled
            data = json.dumps(data, default=pydantic.json.pydantic_encoder)
            data = data.encode()
            data = self.api.plugins.message.sending(operationId=self.operation.operationId, sending=data).sending
            self.req.content = data
            self.req.headers["Content-Type"] = "application/json"
        #        elif "multipart/form-data" in self.operation.requestBody.content:
        #            """
        #            https://swagger.io/docs/specification/describing-request-body/multipart-requests/
        #            """
        #            pass
        #        elif "multipart/mixed" in self.operation.requestBody.content:
        #            pass
        #        elif "multipart/form-data" in self.operation.requestBody.content:
        #            pass

        else:
            raise NotImplementedError()

    def _prepare(self, data, parameters):
        self._prepare_security()
        self._prepare_parameters(parameters)
        self._prepare_body(data)

    def _build_req(self, session):
        req = session.build_request(
            self.method,
            str(self.api.url / self.req.url[1:]),
            headers=self.req.headers,
            cookies=self.req.cookies,
            params=self.req.params,
            content=self.req.content,
            data=self.req.data,
            files=self.req.files,
        )
        return req

    def _process(self, result):
        rheaders = dict()
        # spec enforces these are strings
        status_code = str(result.status_code)
        content_type = result.headers.get("Content-Type", None)

        ctx = self.api.plugins.message.received(
            operationId=self.operation.operationId,
            received=result.content,
            headers=result.headers,
            status_code=status_code,
            content_type=content_type,
        )

        status_code = ctx.status_code
        content_type = ctx.content_type
        headers = ctx.headers

        # find the response model in spec we received
        expected_response = None
        if status_code in self.operation.responses:
            expected_response = self.operation.responses[status_code]
        elif "default" in self.operation.responses:
            expected_response = self.operation.responses["default"]

        if expected_response is None:
            # TODO - custom exception class that has the response object in it
            options = ",".join(self.operation.responses.keys())
            raise HTTPStatusError(
                self.operation,
                result.status_code,
                f"""Unexpected response {result.status_code} from {self.operation.operationId} (expected one of {options}), no default is defined""",
                result,
            )

        if expected_response.headers:
            required = frozenset(
                map(lambda x: x[0].lower(), filter(lambda x: x[1].required is True, expected_response.headers.items()))
            )
            available = frozenset(headers.keys())
            if required - available:
                raise ValueError(f"missing Header {sorted(required - available)}")
            for name, header in expected_response.headers.items():
                data = headers.get(name, None)
                if data:
                    rheaders[name] = header.schema_.model(header._decode(data))

        # status_code == 204 should match here
        if len(expected_response.content) == 0:
            return rheaders, None

        if content_type:
            content_type, _, encoding = content_type.partition(";")
            expected_media = expected_response.content.get(content_type, None)
            if expected_media is None and "/" in content_type:
                # accept media type ranges in the spec. the most specific matching
                # type should always be chosen, but if we do not have a match here
                # a generic range should be accepted if one if provided
                # https://github.com/OAI/OpenAPI-Specification/blob/main/versions/3.0.3.md#response-object

                generic_type = content_type.split("/")[0] + "/*"
                expected_media = expected_response.content.get(generic_type, None)
        else:
            expected_media = None

        if expected_media is None:
            options = ",".join(expected_response.content.keys())
            raise ContentTypeError(
                self.operation,
                content_type,
                f"Unexpected Content-Type {content_type} returned for operation {self.operation.operationId} \
                         (expected one of {options})",
                result,
            )

        if content_type.lower() == "application/json":

            data = ctx.received
            try:
                data = json.loads(data)
            except json.decoder.JSONDecodeError:
                raise ResponseDecodingError(self.operation, result, data)
            data = self.api.plugins.message.parsed(
                operationId=self.operation.operationId,
                parsed=data,
                expected_type=getattr(expected_media.schema_, "_target", expected_media.schema_),
            ).parsed

            if expected_media.schema_ is None:
                raise ResponseSchemaError(self.operation, expected_media, expected_media.schema_, result, None)

            try:
                data = expected_media.schema_.model(data)
            except pydantic.ValidationError as e:
                raise ResponseSchemaError(self.operation, expected_media, expected_media.schema_, result, e)
            except pydantic.errors.ConfigError as e1:
                raise ResponseSchemaError(self.operation, expected_media, expected_media.schema_, result, e1)

            data = self.api.plugins.message.unmarshalled(
                operationId=self.operation.operationId, unmarshalled=data
            ).unmarshalled
            return rheaders, data
        else:
            raise NotImplementedError()


class AsyncRequest(Request, AsyncRequestBase):
    pass
