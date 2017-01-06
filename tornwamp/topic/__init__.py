"""
Used to handle PubSub topics publishers and subscribers
"""
from tornado import gen
from tornado.ioloop import IOLoop
import tornadis

from tornwamp import messages
from tornwamp.topic import customize
from tornwamp.identifier import create_global_id


PUBSUB_TIMEOUT = 60


class RedisUnavailableError(Exception):
    pass


class TopicsManager(dict):
    """
    Manages all existing topics to which connections can potentially
    publish and/or subscribe to.
    """

    @gen.coroutine
    def add_subscriber(self, topic_name, connection, subscription_id=None):
        """
        Add a connection as a topic's subscriber.
        """
        new_topic = Topic(topic_name)
        topic = self.get(topic_name, new_topic)
        subscription_id = subscription_id or create_global_id()
        yield topic.add_subscriber(subscription_id, connection)
        self[topic_name] = topic
        connection.add_subscription_channel(subscription_id, topic_name)
        raise gen.Return(subscription_id)

    def remove_subscriber(self, topic_name, subscription_id):
        """
        Remove a connection a topic's subscriber provided:
        - topic_name
        - subscription_id
        """
        topic = self.get(topic_name)
        if topic and subscription_id in topic.subscribers:
            connection = topic.subscribers.pop(subscription_id)
            connection.remove_subscription_channel(topic_name)

    def add_publisher(self, topic_name, connection, subscription_id=None):
        """
        Add a connection as a topic's publisher.
        """
        topic = self.get(topic_name, Topic(topic_name))
        subscription_id = subscription_id or create_global_id()
        topic.publishers[subscription_id] = connection
        self[topic_name] = topic
        connection.add_publishing_channel(subscription_id, topic_name)
        return subscription_id

    def remove_publisher(self, topic_name, subscription_id):
        """
        Remove a connection a topic's subscriber
        """
        topic = self.get(topic_name)
        if topic and subscription_id in topic.publishers:
            connection = topic.publishers.pop(subscription_id)
            connection.remove_publishing_channel(topic_name)

    def remove_connection(self, connection):
        """
        Connection is to be removed, scrap all connection publishers/subscribers in every topic
        """
        for topic_name, subscription_id in connection.topics.get("publisher", {}).items():
            topic = self.get(topic_name)
            topic.publishers.pop(subscription_id, None)

        for topic_name, subscription_id in connection.topics.get("subscriber", {}).items():
            topic = self.get(topic_name)
            topic.subscribers.pop(subscription_id, None)

    def get_connection(self, topic_name, subscription_id):
        """
        Get topic connection provided topic_name and subscription_id. Try to find
        it in subscribers, otherwise, fetches from publishers.
        Return None if it is not available in any.
        """
        topic = self[topic_name]
        return topic.subscribers.get(subscription_id) or topic.publishers.get(subscription_id)

    @property
    def dict(self):
        """
        Return a dict that is jsonifiable.
        """
        return {k: topic.dict for k, topic in self.items()}


topics = TopicsManager()


class Topic(object):
    """
    Represent a topic, containing its name, subscribers and publishers.
    """
    def __init__(self, name, redis=None):
        self.name = name
        self.subscribers = {}
        self.publishers = {}
        self.redis_params = redis
        if self.redis_params is not None:
            self._publisher_connection = tornadis.Client(ioloop=IOLoop.current(), autoconnect=True, **self.redis_params)
        else:
            self._publisher_connection = None
        self._subscriber_connection = None

    @property
    def connections(self):
        """
        Return a set of topic connections - no matter if they are subscribers or publishers.
        """
        # About merging two dictionaries without changing the original one:

        # first version:
        # dict(self.subscribers, **self.publishers)
        # but it doesn't work with Python 3.5 if keyword args aren't strings

        # cool fancy Python3.5 way:
        # {**self.subscribers, **self.publishers}

        # Python 2+3 boring compatible way:
        conns = self.subscribers.copy()
        conns.update(self.publishers)
        return conns

    @property
    def dict(self):
        """
        Return a dict that is jsonifiable.
        """
        subscribers = {subscription_id: conn.dict for subscription_id, conn in self.subscribers.items()}
        publishers = {subscription_id: conn.dict for subscription_id, conn in self.publishers.items()}
        data = {
            "name": self.name,
            "subscribers": subscribers,
            "publishers": publishers
        }
        return data

    @gen.coroutine
    def publish(self, broadcast_msg):
        """
        Publish event_msg to all subscribers. This method will publish to
        redis if redis is available. Otherwise, it will run locally and can
        be called without yield.

        The parameter publisher_connection_id is used to not publish the
        message back to the publisher.
        """
        event_msg = broadcast_msg.event_message
        customize.deliver_event_messages(self, event_msg, broadcast_msg.publisher_connection_id)
        if self._publisher_connection is not None:
            ret = yield self._publisher_connection.call("PUBLISH", self.name, broadcast_msg.json)
            if isinstance(ret, tornadis.ConnectionError):
                raise RedisUnavailableError(ret)
            raise gen.Return(ret)

    def remove_subscriber(self, subscriber_id):
        """
        Removes subscriber from topic
        """
        subscriber = self.subscribers.pop(subscriber_id)
        subscriber.remove_subscription_channel(self.name)
        return subscriber

    @gen.coroutine
    def add_subscriber(self, subscription_id, connection):
        """
        Add subscriber to a topic. It will register in redis if it is
        available (ie. redis parameter was passed to the constructor),
        otherwise, it will be a simple in memory operation only.
        """
        if self.redis_params is not None and self._subscriber_connection is None:
            self._subscriber_connection = tornadis.PubSubClient(autoconnect=False, ioloop=IOLoop.current(), **self.redis_params)

            ret = yield self._subscriber_connection.connect()
            if not ret:
                raise RedisUnavailableError(ret)

            try:
                ret = yield self._subscriber_connection.pubsub_subscribe(self.name)
            except TypeError:
                # workaround tornadis bug
                # (https://github.com/thefab/tornadis/pull/39)
                # This can be reached in Python 3.x
                raise RedisUnavailableError(str(self.redis_params))
            if not ret:
                # this will only be reached when the previously mentioned bug
                # is fixed
                # This can be reached in Python 2.7
                raise RedisUnavailableError(ret)

            self._register_redis_callback()
        self.subscribers[subscription_id] = connection

    def _register_redis_callback(self):
        """
        Listens for new messages. If connection was dropped, then disconnect
        all subscribers.
        """
        if self._subscriber_connection is not None and self._subscriber_connection.is_connected():
            future = self._subscriber_connection.pubsub_pop_message(deadline=PUBSUB_TIMEOUT)
            IOLoop.current().add_future(future, self._on_redis_message)
        else:
            # Connection with redis was lost
            self._drop_subscribers()

    def _drop_subscribers(self):
        """
        Drop all subscribers of this topic. This is called when connection to
        redis is lost.

        In case we get disconnected from redis we want to drop the connection
        of all subscribers so they know this happened.  Otherwise,
        subscribers could unknowingly lose messages.
        """
        for subscriber_id in list(self.subscribers.keys()):
            subscriber = self.remove_subscriber(subscriber_id)
            subscriber._websocket.close()

    def _on_event_message(self, topic_name, raw_msg):
        msg = messages.BroadcastMessage.from_text(raw_msg.decode("utf-8"))
        assert_msg = "broadcast message topic and redis pub/sub queue must match ({} != {})".format(topic_name, msg.topic_name)
        assert topic_name == msg.topic_name, assert_msg
        if msg.publisher_node_id != messages.PUBLISHER_NODE_ID.hex:
            customize.deliver_event_messages(self, msg.event_message, None)

    def _on_redis_message(self, fut):
        result = fut.result()
        if isinstance(result, tornadis.ConnectionError) or isinstance(result, tornadis.ClientError):
            # Connection with redis was lost
            self._drop_subscribers()
        elif result is not None:
            self._register_redis_callback()
            type_, topic_name, raw_msg = result
            assert type_.decode("utf-8") == u"message", "got wrong message type from pop_message: {}".format(type_)
            self._on_event_message(topic_name.decode('utf-8'), raw_msg)
        else:
            self._register_redis_callback()
