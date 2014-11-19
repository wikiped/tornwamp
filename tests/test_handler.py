from tornado.concurrent import Future
from tornado.httpclient import HTTPRequest
from tornado.testing import AsyncHTTPTestCase, gen_test
from tornado.web import Application
from tornado.websocket import websocket_connect

from tornwamp.handler import WAMPHandler
from tornwamp.messages import ABORT, AbortMessage, HelloMessage, Message, WELCOME
# tornwamp.handler         34      3      4      2    87%   49, 83-84


class UnauthorizeWAMPHandler(WAMPHandler):

    def authorize(self):
        return False, {}, "Denied"


class WAMPHandlerTestCase(AsyncHTTPTestCase):

    def get_app(self):
        application = Application([
            (r"/ws", WAMPHandler),
            (r"/no", UnauthorizeWAMPHandler)
        ])
        return application

    def build_request(self, path="ws", **headers):
        port = self.get_http_port()
        url = 'ws://0.0.0.0:{0}/{1}'.format(port, path)
        if not 'Origin' in headers:
            headers['Origin'] = 'http://0.0.0.0:%d' % port
        headers['Sec-WebSocket-Protocol'] = 'wamp.2.json'
        return HTTPRequest(url, headers=headers)

    @gen_test
    def test_connection_succeeds(self):
        request = self.build_request()
        ws = yield websocket_connect(request)
        msg = HelloMessage(realm="burger.friday")
        ws.write_message(msg.json)

        response = yield ws.read_message()
        message = Message.from_text(response)
        self.assertIs(message.code, WELCOME)
        self.assertFalse(getattr(ws, "close_code", False))
        ws.close()

    @gen_test
    def test_authorization_fails(self):
        request = self.build_request(path="no")
        ws = yield websocket_connect(request)
        text = yield ws.read_message()

        message = AbortMessage.from_text(text)
        self.assertIs(message.code, ABORT)
        self.assertEqual(message.reason, 'tornwamp.error.unauthorized')
        self.assertEqual(message.details['message'], "Denied")

        msg = yield ws.read_message()
        self.assertIs(msg, None)
        self.assertEqual(ws.close_code, 1)
        self.assertEqual(ws.close_reason, "Denied")
