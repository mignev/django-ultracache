"""Monitor service to consume RabbitMQ messages. This twisted plugin typically
runs on each node that runs nginx."""

import pika
import treq
import yaml
from pika.adapters import twisted_connection
from twisted.application import service
from twisted.internet import defer, reactor, protocol
from twisted.internet.defer import inlineCallbacks, returnValue, CancelledError
from twisted.internet.error import ConnectError, DNSLookupError
from twisted.web.client import ResponseFailed


class PurgeService(service.Service):
    """Receive and pass on messages from a RabbitMQ exchange"""

    def __init__(self, options, *args, **kwargs):
        config_file = options.get("config")
        self.config = {}
        if config_file:
            self.config = yaml.load(open(config_file))
        return super(PurgeService, self).__init__(*args, **kwargs)

    def connect(self):
        parameters = pika.ConnectionParameters()
        cc = protocol.ClientCreator(
            reactor, twisted_connection.TwistedProtocolConnection, parameters)
        d = cc.connectTCP(
            self.config.get("rabbit-host", "localhost"),
            self.config.get("rabbit-port", 5672)
        )
        d.addCallback(lambda protocol: protocol.ready)
        d.addCallback(self.setup_connection)
        return d

    @defer.inlineCallbacks
    def setup_connection(self, connection):
        self.channel = yield connection.channel()
        yield self.channel.exchange_declare(exchange="purgatory", type="fanout")
        result = yield self.channel.queue_declare(exclusive=True)
        queue_name = result.method.queue
        yield self.channel.queue_bind(exchange="purgatory", queue=queue_name)
        yield self.channel.basic_qos(prefetch_count=1)
        self.queue_object, self.consumer_tag = yield self.channel.basic_consume(
            queue=queue_name, no_ack=False, exclusive=True)

    @defer.inlineCallbacks
    def process_queue(self):
        while True:
            thing = yield self.queue_object.get()
            if thing is None:
                break
            ch, method, properties, body = thing
            if body:
                path = body
                print "PURGE %s" % path
                try:
                    response = yield treq.request(
                        "PURGE", "http://" \
                            + self.config.get("nginx-host", "127.0.0.1") + path,
                        #cookies={"foo": "bar"},
                        headers={"Host": self.config.get("domain", "actual.host.com")},
                        timeout=10
                    )
                except (ConnectError, DNSLookupError, CancelledError, ResponseFailed):
                    # Maybe better to do a blank except?
                    print "ERROR %s" % path
                else:
                    print "RESULT %s" % path
                    content = yield response.content()
                    print content

            yield ch.basic_ack(delivery_tag=method.delivery_tag)

    @defer.inlineCallbacks
    def startService(self):
        self.running = 1
        yield self.connect()
        self.process_d = self.process_queue()

    @defer.inlineCallbacks
    def stopService(self):
        if not self.running:
            return
        yield self.channel.basic_cancel(callback=None, consumer_tag=self.consumer_tag)
        self.queue_object.put(None)
        yield self.process_d
        self.running = 0


def makeService(options):
    return PurgeService(options)