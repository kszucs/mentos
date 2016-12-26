from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import logging
import os
from os import environ as env
import signal
from threading import Thread
import time
import uuid

from tornado import gen
from tornado.escape import json_decode as decode
from tornado.escape import json_encode as encode
from tornado.httpclient import AsyncHTTPClient
from tornado.httpclient import HTTPClient
from tornado.httpclient import HTTPError
from tornado.httpclient import HTTPRequest

from malefico.core.connection import MesosConnection
from malefico.core.utils import BacklogClient
from malefico.core.utils import decode_data
from malefico.core.utils import encode_data
from malefico.core.utils import log_errors
from malefico.core.utils import parse_duration

log = logging.getLogger(__name__)


class MesosExecutorDriver(MesosConnection):

    def __init__(self, executor, loop=None):

        self.agent_endpoint = "http://"+env['MESOS_AGENT_ENDPOINT']
        super(MesosExecutorDriver, self).__init__(
            leading_master=self.agent_endpoint, loop=loop)

        self.framework_id = dict(value=env['MESOS_FRAMEWORK_ID'])
        self.executor_id = dict(value=env['MESOS_EXECUTOR_ID'])

        grace_shutdown_period = env.get('MESOS_EXECUTOR_SHUTDOWN_GRACE_PERIOD')
        if grace_shutdown_period:
            self.grace_shutdown_period = parse_duration(grace_shutdown_period)
        else:
            self.grace_shutdown_period = 0.0

        self.checkpoint = bool(env.get('MESOS_CHECKPOINT'))
        self.local = bool(env.get('MESOS_LOCAL'))

        self.executor = executor
        self.framework_info = None
        self.executor_info = None
        self.tasks = {}
        self.updates = {}

        self.executor = executor
        self.outbound_connection = AsyncHTTPClient(self.loop)
        self._handlers = {
            "SUBSCRIBED": self.on_subscribed,
            "MESSAGE": self.on_message,
            "LAUNCH": self.on_launch,
            "LAUNCH_GROUP": self.on_launch_group,
            "KILL": self.on_kill,
            "ACKNOWLEDGED": self.on_acknowledged,
            "SHUTDOWN": self.on_shutdown,
            "ERROR": self.on_error,
            "CLOSE": self.on_close
        }

    def gen_request(self, handler):
        payload = encode({
            'type': 'SUBSCRIBE',
            'framework_id': self.framework_id,
            'executor_id': self.executor_id,
            'subscribe': {
                'unacknowledged_tasks': list(self.tasks.values()),
                'unacknowledged_updates': list(self.updates.values()),
            }
        })
        headers = {
            'content-type': 'application/json',
            'accept': 'application/json',
            'connection': 'close'
        }

        subscription_r = HTTPRequest(url=self.leading_master + "/api/v1/executor",
                                     method='POST',
                                     headers=headers,
                                     body=payload,
                                     streaming_callback=self._handlechunks,
                                     header_callback=handler,
                                     follow_redirects=False,
                                     request_timeout=1e15)
        return subscription_r

    def _send(self, payload):

        data = encode(payload)

        def handle_response(response):
            if response.code not in (200, 202):
                log.warn("Problem with request to agent. %s" % response)

        headers = {
            'content-type': 'application/json',
            'accept': 'application/json',
            'connection': 'close'
        }
        self.outbound_connection.fetch(
            HTTPRequest(
                url=self.leading_master + "/api/v1/executor",
                body=data,
                method='POST',
                headers=headers,
            ), handle_response
        )

    def update(self, status):
        """
        """
        if 'timestamp' not in status:
            status['timestamp'] = int(time.time())

        if 'uuid' not in status:
            status['uuid'] = encode_data(uuid.uuid4().bytes)

        if 'source' not in status:
            status['source'] = 'SOURCE_EXECUTOR'

        payload = {
            "type": "UPDATE",
            "framework_id": self.framework_id,
            "executor_id": self.executor_id,
            "update": {
                "status": status
            }
        }
        self._send(payload)
        logging.info('Executor sends status update {} for task {}'.format(
            status["state"], status["task_id"]))

    def message(self, message):
        """
        """
        payload = {
            "type": "MESSAGE",
            "framework_id": self.framework_id,
            "executor_id": self.executor_id,
            "message": {
                "data": encode_data(message)
            }
        }
        self._send(payload)
        logging.info('Driver sends framework message {}'.format(message))



    def on_subscribed(self, info):
        executor_info = info['executor_info']
        framework_info = info['framework_info']
        agent_info = info['agent_info']
        assert executor_info['executor_id'] == self.executor_id
        assert framework_info['id'] == self.framework_id

        if self.executor_info is None or self.framework_info is None:
            self.executor_info = executor_info
            self.framework_info = framework_info
            self.executor.on_registered(
                self, executor_info,
                self.framework_info, agent_info
            )
        else:
            self.executor.on_reregistered(self, agent_info)

    def on_close(self):
        if not self.checkpoint:
            if not self.local:
                self._delay_kill()
            self.executor.shutdown(self)
            self.abort()

    def on_launch_group(self, event):
        task_info = event['task']
        task_id = task_info['task_id']['value']
        assert task_id not in self.tasks
        self.tasks[task_id] = task_info
        self.executor.on_launch(self, task_info)

    def on_launch(self, event):
        task_info = event['task']
        task_id = task_info['task_id']['value']
        assert task_id not in self.tasks
        self.tasks[task_id] = task_info
        self.executor.on_launch(self, task_info)

    def on_kill(self, event):
        task_id = event['task_id']
        self.executor.on_kill(self, task_id)

    def on_acknowledged(self, event):
        task_id = event['task_id']['value']
        uuid_ = uuid.UUID(bytes=decode_data(event['uuid']))
        self.updates.pop(uuid_, None)
        self.tasks.pop(task_id, None)

    def on_message(self, event):
        data = event['data']
        self.executor.on_message(self, data)

    def on_error(self, event):
        message = event['message']
        self.executor.on_error(self, message)

    def on_shutdown(self):
        if not self.local:
            self._delay_kill()
        self.executor.on_shutdown(self)
        self.stop()

    @gen.coroutine
    def _handle_events(self, message):
        with log_errors():
            try:
                if message["type"] in self._handlers:
                    _type = message['type']
                    if _type == "SHUTDOWN":
                        self._handlers[_type]()
                    else:
                        self._handlers[_type](message[_type.lower()])

                else:
                    log.warn("Unhandled event %s" % message)
            except Exception as ex:
                log.warn("Problem dispatching event %s" % message)
                log.exception(ex)

    def _delay_kill(self):
        def _():
            try:
                time.sleep(self.grace_shutdown_period)
                os.killpg(0, signal.SIGKILL)
            except Exception:
                log.exception('Failed to force kill executor')

        t = Thread(target=_)
        t.daemon = True
        t.start()

    def __str__(self):
        return '<%s: executor="%s:%s:%s">' % (
            self.__class__.__name__, self.master, self.framework_id,
            self.agent_endpoint)

    __repr__ = __str__
