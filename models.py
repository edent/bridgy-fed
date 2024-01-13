"""Datastore model classes."""
import copy
from datetime import timedelta, timezone
import itertools
import json
import logging
import random
import re
from threading import Lock
from urllib.parse import quote, urlparse

from arroba.util import parse_at_uri
import cachetools
from Crypto.PublicKey import RSA
from flask import g, request
from google.cloud import ndb
from granary import as1, as2, atom, bluesky, microformats2
from granary.source import html_to_text
from oauth_dropins.webutil import util
from oauth_dropins.webutil.appengine_info import DEBUG
from oauth_dropins.webutil.flask_util import error
from oauth_dropins.webutil.models import (
    ComputedJsonProperty,
    JsonProperty,
    StringIdModel,
)
from oauth_dropins.webutil.util import json_dumps, json_loads

import common
from common import add, base64_to_long, long_to_base64, unwrap
import ids

# maps string label to Protocol subclass. populated by ProtocolUserMeta.
# seed with old and upcoming protocols that don't have their own classes (yet).
PROTOCOLS = {'ostatus': None}

# 2048 bits makes tests slow, so use 1024 for them
KEY_BITS = 1024 if DEBUG else 2048
PAGE_SIZE = 20

# auto delete old objects of these types via the Object.expire property
# https://cloud.google.com/datastore/docs/ttl
OBJECT_EXPIRE_TYPES = (
    'post',
    'update',
    'delete',
    'accept',
    'reject',
    'undo',
    None,
)
OBJECT_EXPIRE_AGE = timedelta(days=90)

OPT_OUT_TAGS = frozenset(('#nobot', '#nobridge'))

logger = logging.getLogger(__name__)


class Target(ndb.Model):
    """:class:`protocol.Protocol` + URI pairs for identifying objects.

    These are currently used for:

    * delivery destinations, eg ActivityPub inboxes, webmention targets, etc.
    * copies of :class:`Object`\s and :class:`User`\s elsewhere,
      eg ``at://`` URIs for ATProto records, nevent etc bech32-encoded Nostr ids,
      ATProto user DIDs, etc.

    Used in :class:`google.cloud.ndb.model.StructuredProperty`\s inside
    :class:`Object` and :class:`User`; not stored as top-level entities in the
    datastore.

    ndb implements this by hoisting each property here into a corresponding
    property on the parent entity, prefixed by the StructuredProperty name
    below, eg ``delivered.uri``, ``delivered.protocol``, etc.

    For repeated StructuredPropertys, the hoisted properties are all repeated on
    the parent entity, and reconstructed into StructuredPropertys based on their
    order.

    https://googleapis.dev/python/python-ndb/latest/model.html#google.cloud.ndb.model.StructuredProperty
    """
    uri = ndb.StringProperty(required=True)
    # choices is populated in app via reset_protocol_properties, after all User
    # subclasses are created, so that PROTOCOLS is fully populated
    protocol = ndb.StringProperty(choices=[], required=True)

    def __eq__(self, other):
        """Equality excludes Targets' :class:`Key`."""
        return self.uri == other.uri and self.protocol == other.protocol

    def __hash__(self):
        """Allow hashing so these can be dict keys."""
        return hash((self.protocol, self.uri))


class ProtocolUserMeta(type(ndb.Model)):
    """:class:`User` metaclass. Registers all subclasses in the ``PROTOCOLS`` global."""
    def __new__(meta, name, bases, class_dict):
        cls = super().__new__(meta, name, bases, class_dict)

        if hasattr(cls, 'LABEL') and cls.LABEL not in ('protocol', 'user'):
            for label in (cls.LABEL, cls.ABBREV) + cls.OTHER_LABELS:
                if label:
                    PROTOCOLS[label] = cls

        return cls


def reset_protocol_properties():
    """Recreates various protocol properties to include choices from ``PROTOCOLS``."""
    Target.protocol = ndb.StringProperty(
        'protocol', choices=list(PROTOCOLS.keys()), required=True)
    Object.source_protocol = ndb.StringProperty(
        'source_protocol', choices=list(PROTOCOLS.keys()))

    abbrevs = f'({"|".join(PROTOCOLS.keys())}|fed)'
    common.SUBDOMAIN_BASE_URL_RE = re.compile(
        rf'^https?://({abbrevs}\.brid\.gy|localhost(:8080)?)/(convert/|r/)?({abbrevs}/)?')


class User(StringIdModel, metaclass=ProtocolUserMeta):
    """Abstract base class for a Bridgy Fed user.

    Stores some protocols' keypairs. Currently:

    * RSA keypair for ActivityPub HTTP Signatures
      properties: ``mod``, ``public_exponent``, ``private_exponent``, all
      encoded as base64url (ie URL-safe base64) strings as described in RFC
      4648 and section 5.1 of the Magic Signatures spec:
      https://tools.ietf.org/html/draft-cavage-http-signatures-12
    * *Not* K-256 signing or rotation keys for AT Protocol, those are stored in
      :class:`arroba.datastore_storage.AtpRepo` entities
    """
    obj_key = ndb.KeyProperty(kind='Object')  # user profile
    mod = ndb.StringProperty()
    use_instead = ndb.KeyProperty()

    # Proxy copies of this user elsewhere, eg DIDs for ATProto records, bech32
    # npub Nostr ids, etc. Similar to rel-me links in microformats2, alsoKnownAs
    # in DID docs (and now AS2), etc.
    # TODO: switch to using Object.copies on the user profile object?
    copies = ndb.StructuredProperty(Target, repeated=True)

    # whether this user signed up or otherwise explicitly, deliberately
    # interacted with Bridgy Fed. For example, if fediverse user @a@b.com looks
    # up @foo.com@fed.brid.gy via WebFinger, we'll create Users for both,
    # @a@b.com will be direct, foo.com will not.
    direct = ndb.BooleanProperty(default=False)

    # these are for ActivityPub HTTP Signatures
    public_exponent = ndb.StringProperty()
    private_exponent = ndb.StringProperty()

    created = ndb.DateTimeProperty(auto_now_add=True)
    updated = ndb.DateTimeProperty(auto_now=True)

    # `existing` attr is set by get_or_create

    # OLD. some stored entities still have this; do not reuse.
    # actor_as2 = JsonProperty()

    def __init__(self, **kwargs):
        """Constructor.

        Sets :attr:`obj` explicitly because however
        :class:`google.cloud.ndb.model.Model` sets it doesn't work with
        ``@property`` and ``@obj.setter`` below.
        """
        obj = kwargs.pop('obj', None)
        super().__init__(**kwargs)

        if obj:
            self.obj = obj

    @classmethod
    def new(cls, **kwargs):
        """Try to prevent instantiation. Use subclasses instead."""
        raise NotImplementedError()

    def _post_put_hook(self, future):
        logger.info(f'Wrote {self.key}')

    @classmethod
    def get_by_id(cls, id, allow_opt_out=False):
        """Override to follow ``use_instead`` property and ``opt-out` status.

        Returns None if the user is opted out.
        """
        user = cls._get_by_id(id)
        if not user:
            return None
        elif user.use_instead:
            logger.info(f'{user.key} use_instead => {user.use_instead}')
            return user.use_instead.get()
        elif user.status == 'opt-out' and not allow_opt_out:
            logger.info(f'{user.key} is opted out')
            return None

        return user

    @classmethod
    @ndb.transactional()
    def get_or_create(cls, id, propagate=False, **kwargs):
        """Loads and returns a :class:`User`. Creates it if necessary.

        Args:
          propagate (bool): whether to create copies of this user in push-based
            protocols, eg ATProto and Nostr.

        Returns:
          User: existing or new user, or None if the user is opted out
        """
        assert cls != User
        user = cls.get_by_id(id, allow_opt_out=True)
        if user:
            if user.status == 'opt-out':
                return None
            user.existing = True
            # override direct from False => True if set
            # TODO: propagate more props into user?
            direct = kwargs.get('direct')
            if direct and not user.direct:
                logger.info(f'Setting {user.key} direct={direct}')
                user.direct = direct
                user.put()
            if not propagate:
                return user
        else:
            user = cls(id=id, **kwargs)
            user.existing = False

        # load and propagate user and profile object
        if not user.obj_key:
            user.obj = cls.load(user.profile_id())

        ATProto = PROTOCOLS['atproto']
        if propagate and cls.LABEL != 'atproto' and not user.get_copy(ATProto):
            ATProto.create_for(user)

        # generate keys for all protocols _except_ our own
        #
        # these can use urandom() and do nontrivial math, so they can take time
        # depending on the amount of randomness available and compute needed.
        if cls.LABEL != 'activitypub':
            key = RSA.generate(KEY_BITS, randfunc=random.randbytes if DEBUG else None)
            user.mod = long_to_base64(key.n)
            user.public_exponent = long_to_base64(key.e)
            user.private_exponent = long_to_base64(key.d)

        try:
            user.put()
        except AssertionError as e:
            error(f'Bad {cls.__name__} id {id} : {e}')

        logger.info(f'Created new {user}')
        return user

    @property
    def obj(self):
        """Convenience accessor that loads :attr:`obj_key` from the datastore."""
        if self.obj_key:
            if not hasattr(self, '_obj'):
                self._obj = self.obj_key.get()
            return self._obj

    @obj.setter
    def obj(self, obj):
        if obj:
            assert isinstance(obj, Object)
            assert obj.key
            self._obj = obj
            self.obj_key = obj.key
        else:
            self._obj = self.obj_key = None

    @classmethod
    def load_multi(cls, users):
        """Loads :attr:`obj` for multiple users in parallel.

        Args:
          users (sequence of User)
        """
        objs = ndb.get_multi(u.obj_key for u in users if u.obj_key)
        keys_to_objs = {o.key: o for o in objs if o}

        for u in users:
            u._obj = keys_to_objs.get(u.obj_key)

    @ndb.ComputedProperty
    def handle(self):
        """This user's unique, human-chosen handle, eg ``@me@snarfed.org``.

        To be implemented by subclasses.
        """
        raise NotImplementedError()

    @ndb.ComputedProperty
    def readable_id(self):
        """DEPRECATED: replaced by handle. Kept for backward compatibility."""
        return None

    @ndb.ComputedProperty
    def status(self):
        """Whether this user has explicitly opted out of Bridgy Fed.

        Optional. Current possible values:
          * ``opt-out``

        Currently just looks for ``#nobridge`` or ``#nobot`` in the profile
        description/bio.

        Duplicates ``util.is_opt_out`` in Bridgy!

        https://github.com/snarfed/bridgy-fed/issues/666
        """
        if self.key.id() in common.OPT_OUT_IDS:
            return 'opt-out'

        if not self.obj or not self.obj.as1:
            return None

        for field in 'summary', 'displayName':
            text = html_to_text(self.obj.as1.get(field, ''))
            for tag in OPT_OUT_TAGS:
                if tag in text:
                    return 'opt-out'

        return None

    def handle_as(self, to_proto):
        """Returns this user's handle in a different protocol.

        Args:
          to_proto (str or Protocol)

        Returns:
          str
        """
        if isinstance(to_proto, str):
            to_proto = PROTOCOLS[to_proto]

        # override web users to always use domain instead of custom username
        # TODO: fall back to id if handle is unset?
        handle = self.key.id() if self.LABEL == 'web' else self.handle
        if not handle:
            return None

        return ids.translate_handle(handle=handle, from_proto=self.__class__,
                                    to_proto=to_proto, enhanced=False)

    def id_as(self, to_proto):
        """Returns this user's id in a different protocol.

        Args:
          to_proto (str or Protocol)

        Returns:
          str
        """
        if isinstance(to_proto, str):
            to_proto = PROTOCOLS[to_proto]

        return ids.translate_user_id(id=self.key.id(), from_proto=self.__class__,
                                     to_proto=to_proto)

    def handle_or_id(self):
        """Returns handle if we know it, otherwise id."""
        return self.handle or self.key.id()

    def public_pem(self):
        """
        Returns:
          bytes:
        """
        rsa = RSA.construct((base64_to_long(str(self.mod)),
                             base64_to_long(str(self.public_exponent))))
        return rsa.exportKey(format='PEM')

    def private_pem(self):
        """
        Returns:
          bytes:
        """
        assert self.mod and self.public_exponent and self.private_exponent, str(self)
        rsa = RSA.construct((base64_to_long(str(self.mod)),
                             base64_to_long(str(self.public_exponent)),
                             base64_to_long(str(self.private_exponent))))
        return rsa.exportKey(format='PEM')

    def name(self):
        """Returns this user's human-readable name, eg ``Ryan Barrett``."""
        if self.obj and self.obj.as1:
            name = self.obj.as1.get('displayName')
            if name:
                return name

        return self.handle_or_id()

    def web_url(self):
        """Returns this user's web URL (homepage), eg ``https://foo.com/``.

        To be implemented by subclasses.

        Returns:
          str
        """
        raise NotImplementedError()

    def is_web_url(self, url, ignore_www=False):
        """Returns True if the given URL is this user's web URL (homepage).

        Args:
          url (str)
          ignore_www (bool): if True, ignores ``www.`` subdomains

        Returns:
          bool:
        """
        if not url:
            return False

        url = url.strip().rstrip('/')
        url = re.sub(r'^(https?://)www\.', r'\1', url)
        parsed_url = urlparse(url)
        if parsed_url.scheme not in ('http', 'https', ''):
            return False

        this = self.web_url().rstrip('/')
        this = re.sub(r'^(https?://)www\.', r'\1', this)
        parsed_this = urlparse(this)

        return (url == this or url == parsed_this.netloc or
                parsed_url[1:] == parsed_this[1:])  # ignore http vs https

    def profile_id(self):
        """Returns the id of this user's profile object in its native protocol.

        Examples:

        * Web: home page URL, eg ``https://me.com/``
        * ActivityPub: actor URL, eg ``https://instance.com/users/me``
        * ATProto: profile AT URI, eg ``at://did:plc:123/app.bsky.actor.profile/self``

        Defaults to this user's key id.

        Returns:
          str:
        """
        return self.key.id()

    def user_page_path(self, rest=None):
        """Returns the user's Bridgy Fed user page path."""
        path = f'/{self.ABBREV}/{self.handle_or_id()}'

        if rest:
            if not rest.startswith('?'):
                path += '/'
            path += rest

        return path

    def get_copy(self, proto):
        """Returns the id for the copy of this user in a given protocol.

        ...or None if no such copy exists. If ``proto`` is this user, returns
        this user's key id.

        Args:
          proto: :class:`Protocol` subclass

        Returns:
          str:
        """
        if isinstance(self, proto):
            return self.key.id()

        for copy in self.copies:
            if copy.protocol in (proto.LABEL, proto.ABBREV):
                return copy.uri

    def user_link(self):
        """Returns a pretty link to the external user with name and profile picture."""
        actor = self.obj.as1 if self.obj and self.obj.as1 else {}
        img = util.get_url(actor, 'image') or ''
        return f"""\
<a class="h-card u-author" href="{self.web_url()}">
  <img src="{img}" class="profile">
  {self.name()}</a>"""

    @cachetools.cached(cachetools.TTLCache(50000, 60 * 60 * 2),  # 2h expiration
                       key=lambda user: user.key.id(), lock=Lock())
    def count_followers(self):
        """Counts this user's followers and followings.

        Returns:
          (int, int) tuple: (number of followers, number following)
        """
        num_followers = Follower.query(Follower.to == self.key,
                                       Follower.status == 'active')\
                                .count()
        num_following = Follower.query(Follower.from_ == self.key,
                                       Follower.status == 'active')\
                                .count()
        return num_followers, num_following


class Object(StringIdModel):
    """An activity or other object, eg actor.

    Key name is the id. We synthesize ids if necessary.
    """
    STATUSES = ('new', 'in progress', 'complete', 'failed', 'ignored')
    LABELS = ('activity',
              # DEPRECATED, replaced by users, notify, feed
              'feed', 'notification', 'user')

    # Keys for user(s) who created or otherwise own this activity.
    #
    # DEPRECATED: this used to include all users related the activity, including
    # followers, but we've now moved those to the notify and feed properties.
    users = ndb.KeyProperty(repeated=True)
    # User keys who should see this activity in their user page, eg in reply to,
    # reaction to, share of, etc.
    notify = ndb.KeyProperty(repeated=True)
    # User keys who should see this activity in their feeds, eg followers of its
    # creator
    feed = ndb.KeyProperty(repeated=True)

    # DEPRECATED but still used read only to maintain backward compatibility
    # with old Objects in the datastore that we haven't bothered migrating.
    domains = ndb.StringProperty(repeated=True)

    status = ndb.StringProperty(choices=STATUSES)
    # choices is populated in app, after all User subclasses are created,
    # so that PROTOCOLS is fully populated
    # TODO: nail down whether this is ABBREV or LABEL
    source_protocol = ndb.StringProperty(choices=[])
    labels = ndb.StringProperty(repeated=True, choices=LABELS)

    # TODO: switch back to ndb.JsonProperty if/when they fix it for the web console
    # https://github.com/googleapis/python-ndb/issues/874
    as2 = JsonProperty()      # only one of the rest will be populated...
    bsky = JsonProperty()     # Bluesky / AT Protocol
    mf2 = JsonProperty()      # HTML microformats2 item (ie _not_ the top level
                              # parse object with items inside an 'items' field)
    our_as1 = JsonProperty()  # AS1 for activities that we generate or modify ourselves
    raw = JsonProperty()      # other standalone data format, eg DID document

    # these are full feeds with multiple items, not just this one, so they're
    # stored as audit records only. they're not used in to_as1. for Atom/RSS
    # based Objects, our_as1 will be populated with an feed_index top-level
    # integer field that indexes into one of these.
    atom = ndb.TextProperty() # Atom XML
    rss = ndb.TextProperty()  # RSS XML

    deleted = ndb.BooleanProperty()

    delivered = ndb.StructuredProperty(Target, repeated=True)
    undelivered = ndb.StructuredProperty(Target, repeated=True)
    failed = ndb.StructuredProperty(Target, repeated=True)

    # Copies of this object elsewhere, eg at:// URIs for ATProto records and
    # nevent etc bech32-encoded Nostr ids, where this object is the original.
    # Similar to u-syndication links in microformats2 and
    # upstream/downstreamDuplicates in AS1.
    copies = ndb.StructuredProperty(Target, repeated=True)

    created = ndb.DateTimeProperty(auto_now_add=True)
    updated = ndb.DateTimeProperty(auto_now=True)

    new = None
    changed = None
    """Protocol and subclasses set these in fetch if this :class:`Object` is
    new or if its contents have changed from what was originally loaded from the
    datastore. If either one is None, that means we don't know whether this
    :class:`Object` is new/changed.

    :attr:`changed` is populated by :meth:`activity_changed()`.
    """

    lock = None
    """Initialized in __init__, synchronizes property access, :meth:`put`s, etc."""

    @ComputedJsonProperty
    def as1(self):
        def use_urls_as_ids(obj):
            """If id field is missing or not a URL, use the url field."""
            id = obj.get('id')
            if not id or not util.is_web(id):
                if url := util.get_url(obj):
                    obj['id'] = url

            for field in 'author', 'actor', 'object':
                if inner := as1.get_object(obj, field):
                    use_urls_as_ids(inner)

        if self.our_as1:
            obj = self.our_as1
            if self.atom or self.rss:
                use_urls_as_ids(obj)

        elif self.as2:
            obj = as2.to_as1(self.as2)

        elif self.bsky:
            owner, _, _ = parse_at_uri(self.key.id())
            ATProto = PROTOCOLS['atproto']
            handle = ATProto(id=owner).handle
            obj = bluesky.to_as1(self.bsky, repo_did=owner, repo_handle=handle,
                                 uri=self.key.id(), pds=ATProto.target_for(self))

        elif self.mf2:
            obj = microformats2.json_to_object(self.mf2,
                                               rel_urls=self.mf2.get('rel-urls'))
            use_urls_as_ids(obj)

        else:
            return None

        # populate id if necessary
        if self.key:
            obj.setdefault('id', self.key.id())

        return obj

    @ndb.ComputedProperty
    def type(self):  # AS1 objectType, or verb if it's an activity
        if self.as1:
            return as1.object_type(self.as1)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.lock = Lock()

    def _object_ids(self):  # id(s) of inner objects
        if self.as1:
            return unwrap(as1.get_ids(self.as1, 'object'))

    object_ids = ndb.ComputedProperty(_object_ids, repeated=True)

    def _expire(self):
        """Maybe automatically delete this Object after 90d using a TTL policy.

        https://cloud.google.com/datastore/docs/ttl

        They recommend not indexing TTL properties:
        https://cloud.google.com/datastore/docs/ttl#ttl_properties_and_indexes
        """
        if self.type in OBJECT_EXPIRE_TYPES:
            return (self.updated or util.now()) + OBJECT_EXPIRE_AGE

    expire = ndb.ComputedProperty(_expire, indexed=False)

    def _pre_put_hook(self):
        """
        * Validate that at:// URIs have DID repos
        * Set/remove the activity label
        * Strip @context from as2 (we don't do LD) to save disk space
        """
        id = self.key.id()
        assert '^^' not in id

        if self.source_protocol not in (None, 'ui'):
            proto = PROTOCOLS[self.source_protocol]
            assert proto.owns_id(id) is not False, \
                f'Protocol {proto.LABEL} does not own id {id}'

        if id.startswith('at://'):
            repo, _, _ = parse_at_uri(id)
            if not repo.startswith('did:'):
                # TODO: if we hit this, that means the AppView gave us an AT URI
                # with a handle repo/authority instead of DID. that's surprising!
                # ...if so, and if we need to handle it, add a new
                # arroba.did.canonicalize_at_uri() function, then use it here,
                # or before.
                raise ValueError(
                    f'at:// URI ids must have DID repos; got {id}')

        if self.as1 and self.as1.get('objectType') == 'activity':
            # can't self.add because we're inside self.put, which has the lock
            add(self.labels, 'activity')
        elif 'activity' in self.labels:
            # ditto
            self.labels.remove('activity')

        if self.as2:
           self.as2.pop('@context', None)
           for field in 'actor', 'attributedTo', 'author', 'object':
               for val in util.get_list(self.as2, field):
                   if isinstance(val, dict):
                       val.pop('@context', None)

    def _post_put_hook(self, future):
        """Update :meth:`Protocol.load` cache."""
        # TODO: assert that as1 id is same as key id? in pre put hook?

        # log, pruning data fields
        props = util.trim_nulls({
            **self.to_dict(),
            'new': self.new,
            'changed': self.changed,
        })
        for prop in 'as2', 'atom', 'bsky', 'mf2', 'our_as1', 'raw', 'rss':
            if props.get(prop):
                props[prop] = "..."
        for prop in 'created', 'updated', 'as1', 'expire':
            props.pop(prop, None)

        logger.info(f'Wrote {self.key} {props}')

        if '#' not in self.key.id():
            import protocol  # TODO: actually fix this circular import
            # make a copy so that if we later modify this object in memory,
            # those modifications don't affect the cache.
            # NOTE: keep in sync with Protocol.load!
            protocol.objects_cache[self.key.id()] = Object(
                id=self.key.id(),
                # exclude computed properties
                **self.to_dict(exclude=['as1', 'expire', 'object_ids', 'type']))

    @classmethod
    def get_by_id(cls, id):
        """Override :meth:`google.cloud.ndb.model.Model.get_by_id` to un-escape
        ``^^`` to ``#``.

        Only needed for compatibility with historical URL paths, we're now back
        to URL-encoding ``#``\s instead.
        https://github.com/snarfed/bridgy-fed/issues/469
        """
        return super().get_by_id(id.replace('^^', '#'))

    @classmethod
    @ndb.transactional()
    def get_or_create(cls, id, actor=None, **props):
        """Returns an :class:`Object` with the given property values.

        If a matching :class:`Object` doesn't exist in the datastore, creates it
        first. Only populates non-False/empty property values in props into the
        object. Also populates the :attr:`new` and :attr:`changed` properties.

        Args:
          actor (str): if a matching :class:`Object` already exists, its
            `author` or `actor` must contain this actor id. Implements basic
            authorization for updates and deletes.

        Returns:
          Object:
        """
        obj = cls.get_by_id(id)
        if obj:
            obj.new = False
            orig_as1 = obj.as1
            if orig_as1:
                authorized = (as1.get_ids(orig_as1, 'author') +
                              as1.get_ids(orig_as1, 'actor'))
                if not actor:
                    logger.warning(f'would cowardly refuse to overwrite {id} without checking actor')
                elif actor not in authorized + [id]:
                    logger.warning(f"actor {actor} isn't {id}'s author or actor {authorized}")
        else:
            obj = Object(id=id)
            obj.new = True

        if set(props.keys()) & set(('as2', 'bsky', 'mf2', 'raw')):
            obj.clear()
        obj.populate(**{
            k: v for k, v in props.items()
            if v and not isinstance(getattr(Object, k), ndb.ComputedProperty)
        })
        if not obj.new:
            obj.changed = obj.activity_changed(orig_as1)

        obj.put()
        return obj

    def put(self, **kwargs):
        """Stores this object. Uses ``self.lock``.
        """
        with self.lock:
            return super().put(**kwargs)

    def add(self, prop, val):
        """Adds a value to a multiply-valued property. Uses ``self.lock``.

        Args:
          prop (str)
          val
        """
        with self.lock:
            add(getattr(self, prop), val)

    def remove(self, prop, val):
        """Removes a value from a multiply-valued property. Uses ``self.lock``.

        Args:
          prop (str)
          val
        """
        with self.lock:
            getattr(self, prop).remove(val)

    def clear(self):
        """Clears the :attr:`Object.our_as1` properties."""
        with self.lock:
            self.our_as1 = None

    def activity_changed(self, other_as1):
        """Returns True if this activity is meaningfully changed from ``other_as1``.

        ...otherwise False.

        Used to populate :attr:`changed`.

        Args:
          other_as1 (dict): AS1 object, or none
        """
        # ignore inReplyTo since we translate it between protocols
        return (as1.activity_changed(self.as1, other_as1, inReplyTo=False)
                if self.as1 and other_as1
                else bool(self.as1) != bool(other_as1))

    def actor_link(self, image=True, sized=False, user=None):
        """Returns a pretty HTML link with the actor's name and picture.

        Args:
          image (bool): whether to include an ``img`` tag with the actor's picture
          sized (bool): whether to set an explicit (``width=32``) size on the
            profile picture ``img` tag
          user (User): current user

        Returns:
          str:
        """
        attrs = {'class': 'h-card u-author'}

        if (self.source_protocol in ('web', 'webmention', 'ui')
                and (user.key in self.users or user.key.id() in self.domains)):
            # outbound; show a nice link to the user
            return user.user_link()

        actor = None
        if self.as1:
            actor = (as1.get_object(self.as1, 'actor')
                     or as1.get_object(self.as1, 'author'))
            # hydrate from datastore if available
            # TODO: optimize! this is called serially in loops, eg in home.html
            if set(actor.keys()) == {'id'} and self.source_protocol:
                proto = PROTOCOLS[self.source_protocol]
                actor_obj = proto.load(actor['id'], remote=False)
                if actor_obj and actor_obj.as1:
                    actor = actor_obj.as1

        if not actor:
            return ''
        elif set(actor.keys()) == {'id'}:
            return common.pretty_link(actor['id'], attrs=attrs, user=user)

        url = as1.get_url(actor)
        name = actor.get('displayName') or actor.get('username') or ''
        img_url = util.get_url(actor, 'image')
        if not image or not img_url:
            return common.pretty_link(url, text=name, attrs=attrs, user=user)

        return f"""\
        <a class="h-card u-author" href="{url}" title="{name}">
          <img class="profile" src="{img_url}" {'width="32"' if sized else ''}/>
          {util.ellipsize(name, chars=40)}
        </a>"""

    def get_copy(self, proto):
        """Returns the id for the copy of this object in a given protocol.

        ...or None if no such copy exists. If ``proto`` is ``source_protocol``,
        returns this object's key id.

        Args:
          proto: :class:`Protocol` subclass

        Returns:
          str:
        """
        if self.source_protocol in (proto.LABEL, proto.ABBREV):
            return self.key.id()

        for copy in self.copies:
            if copy.protocol in (proto.LABEL, proto.ABBREV):
                return copy.uri

    def resolve_ids(self):
        """Resolves "copy" ids, subdomain ids, etc with their originals.

        The end result is that all ids are original "source" ids, ie in the
        protocol that they first came from.

        Specifically, resolves:

        * ids in :class:`User.copies` and :class:`Object.copies`, eg ATProto
          records and Nostr events that we bridged, to the ids of their
          original objects in their source protocol, eg
          ``at://did:plc:abc/app.bsky.feed.post/123`` => ``https://mas.to/@user/456``.
        * Bridgy Fed subdomain URLs to the ids embedded inside them, eg
          ``https://atproto.brid.gy/ap/did:plc:xyz`` => ``did:plc:xyz``

        ...in these AS1 fields, in place:

        * ``id``
        * ``actor``
        * ``author``
        * ``object``
        * ``object.actor``
        * ``object.author``
        * ``object.id``
        * ``object.inReplyTo``
        * ``tags.[objectType=mention].url``

        :meth:`protocol.Protocol.translate_ids` is partly the inverse of this.
        Much of the same logic is duplicated there!
        """
        if not self.as1:
            return

        # extract ids, strip Bridgy Fed subdomain URLs
        outer_obj = unwrap(self.as1)
        if outer_obj != self.as1:
            self.our_as1 = util.trim_nulls(outer_obj)

        self_proto = PROTOCOLS.get(self.source_protocol)
        if not self_proto:
            return

        inner_obj = outer_obj['object'] = as1.get_object(outer_obj)
        fields = ['actor', 'author', 'inReplyTo']

        # collect relevant ids
        ids = [inner_obj.get('id')]
        for obj in outer_obj, inner_obj:
            for tag in as1.get_objects(obj, 'tags'):
                if tag.get('objectType') == 'mention':
                    ids.append(tag.get('url'))
            for field in fields:
                for val in as1.get_objects(obj, field):
                    ids.append(val.get('id'))

        ids = util.trim_nulls(ids)
        if not ids:
            return

        # batch lookup matching users
        origs = {}  # maps str copy URI to str original URI
        for obj in get_originals(ids):
            for copy in obj.copies:
                if copy.protocol in (self_proto.LABEL, self_proto.ABBREV):
                    origs[copy.uri] = obj.key.id()

        logger.debug(f'Replacing copies with originals: {origs}')
        replaced = False

        def replace(val):
            id = val.get('id') if isinstance(val, dict) else val
            orig = origs.get(id)
            if not orig:
                return val

            nonlocal replaced
            replaced = True
            if isinstance(val, dict) and val.keys() > {'id'}:
                val['id'] = orig
                return val
            else:
                return orig

        # actually replace ids
        for obj in outer_obj, inner_obj:
            for tag in as1.get_objects(obj, 'tags'):
                if tag.get('objectType') == 'mention':
                    tag['url'] = replace(tag.get('url'))
            for field in fields:
                obj[field] = [replace(val) for val in util.get_list(obj, field)]
                if len(obj[field]) == 1:
                    obj[field] = obj[field][0]

        outer_obj['object'] = replace(inner_obj)
        if util.trim_nulls(outer_obj['object']).keys() == {'id'}:
            outer_obj['object'] = outer_obj['object']['id']

        if replaced:
            self.our_as1 = util.trim_nulls(outer_obj)


class Follower(ndb.Model):
    """A follower of a Bridgy Fed user."""
    STATUSES = ('active', 'inactive')

    # these are both subclasses of User
    from_ = ndb.KeyProperty(name='from', required=True)
    to = ndb.KeyProperty(required=True)

    follow = ndb.KeyProperty(Object)  # last follow activity
    status = ndb.StringProperty(choices=STATUSES, default='active')

    created = ndb.DateTimeProperty(auto_now_add=True)
    updated = ndb.DateTimeProperty(auto_now=True)

    # OLD. some stored entities still have these; do not reuse.
    # src = ndb.StringProperty()
    # dest = ndb.StringProperty()
    # last_follow = JsonProperty()

    def _pre_put_hook(self):
        if self.from_.kind() == 'Fake' and self.to.kind() == 'Fake':
            return

        # we're a bridge! stick with bridging.
        assert self.from_.kind() != self.to.kind(), f'from {self.from_} to {self.to}'

    def _post_put_hook(self, future):
        # log, pruning data fields
        props = util.trim_nulls(self.to_dict())
        if props.get('follow'):
            props['follow'] = "..."
        for prop in 'created', 'updated':
            props.pop(prop, None)

        logger.info(f'Wrote {self.key} {props}')

    @classmethod
    @ndb.transactional()
    def get_or_create(cls, *, from_, to, **kwargs):
        """Returns a Follower with the given ``from_`` and ``to`` users.

        If a matching :class:`Follower` doesn't exist in the datastore, creates
        it first.

        Args:
          from_ (User)
          to (User)

        Returns:
          Follower:
        """
        assert from_
        assert to

        follower = Follower.query(Follower.from_ == from_.key,
                                  Follower.to == to.key,
                                  ).get()
        if not follower:
            follower = Follower(from_=from_.key, to=to.key, **kwargs)
            follower.put()
        elif kwargs:
            # update existing entity with new property values, eg to make an
            # inactive Follower active again
            for prop, val in kwargs.items():
                setattr(follower, prop, val)
            follower.put()

        return follower

    @staticmethod
    def fetch_page(collection, user):
        """Fetches a page of :class:`Follower`s for a given user.

        Wraps :func:`fetch_page`. Paging uses the ``before`` and ``after`` query
        parameters, if available in the request.

        Args:
          collection (str): ``followers`` or ``following``
          user (User)

        Returns:
          (list of Follower, str, str) tuple: results, annotated with an extra
          ``user`` attribute that holds the follower or following :class:`User`,
          and new str query param values for ``before`` and ``after`` to fetch
          the previous and next pages, respectively
        """
        assert collection in ('followers', 'following'), collection

        filter_prop = Follower.to if collection == 'followers' else Follower.from_
        query = Follower.query(
            Follower.status == 'active',
            filter_prop == user.key,
        ).order(-Follower.updated)

        followers, before, after = fetch_page(query, Follower, by=Follower.updated)
        users = ndb.get_multi(f.from_ if collection == 'followers' else f.to
                              for f in followers)
        User.load_multi(u for u in users if u)

        for f, u in zip(followers, users):
            f.user = u
        followers = [f for f in followers if f.user.status != 'opt-out']

        return followers, before, after


def fetch_objects(query, by=None, user=None):
    """Fetches a page of :class:`Object` entities from a datastore query.

    Wraps :func:`fetch_page` and adds attributes to the returned
    :class:`Object` entities for rendering in ``objects.html``.

    Args:
      query (ndb.Query)
      by (ndb.model.Property): either :attr:`Object.updated` or
        :attr:`Object.created`
      user (User): current user

    Returns:
      (list of Object, str, str) tuple:
      (results, new ``before`` query param, new ``after`` query param)
      to fetch the previous and next pages, respectively
    """
    assert by is Object.updated or by is Object.created
    objects, new_before, new_after = fetch_page(query, Object, by=by)
    objects = [o for o in objects if not o.deleted]

    # synthesize human-friendly content for objects
    for i, obj in enumerate(objects):
        if obj.deleted:
            continue

        obj_as1 = obj.as1
        inner_obj = as1.get_object(obj_as1)

        # synthesize text snippet
        type = as1.object_type(obj_as1)
        if type == 'post':
            inner_type = inner_obj.get('objectType')
            if inner_type:
                type = inner_type

        phrases = {
            'article': 'posted',
            'comment': 'replied',
            'delete': 'deleted',
            'follow': 'followed',
            'invite': 'is invited to',
            'issue': 'filed issue',
            'like': 'liked',
            'note': 'posted',
            'post': 'posted',
            'repost': 'reposted',
            'rsvp-interested': 'is interested in',
            'rsvp-maybe': 'might attend',
            'rsvp-no': 'is not attending',
            'rsvp-yes': 'is attending',
            'share': 'reposted',
            'stop-following': 'unfollowed',
            'undo': 'undid',
            'update': 'updated',
        }
        obj.phrase = phrases.get(type)

        content = (inner_obj.get('content')
                   or inner_obj.get('displayName')
                   or inner_obj.get('summary'))
        if content:
            content = util.parse_html(content).get_text()

        urls = as1.object_urls(inner_obj)
        id = unwrap(inner_obj.get('id', ''))
        url = urls[0] if urls else id
        if (type == 'update' and
            (obj.users and (user.is_web_url(id)
                            or id.strip('/') == obj.users[0].id())
             or obj.domains and id.strip('/') == f'https://{obj.domains[0]}')):
            obj.phrase = 'updated'
            obj_as1.update({
                'content': 'their profile',
                'url': id,
            })
        elif url:
            # heuristics for sniffing Mastodon and similar fediverse URLs and
            # converting them to more friendly @-names
            # TODO: standardize this into granary.as2 somewhere?
            if not content:
                fedi_url = re.match(
                    r'https://[^/]+/(@|users/)([^/@]+)(@[^/@]+)?(/(?:statuses/)?[0-9]+)?', url)
                if fedi_url:
                    content = '@' + fedi_url.group(2)
                    if fedi_url.group(4):
                        content += "'s post"
            content = common.pretty_link(url, text=content, user=user)

        obj.content = (obj_as1.get('content')
                       or obj_as1.get('displayName')
                       or obj_as1.get('summary'))
        obj.url = util.get_first(obj_as1, 'url')

        if type in ('like', 'follow', 'repost', 'share') or not obj.content:
            if obj.url:
                obj.phrase = common.pretty_link(
                    obj.url, text=obj.phrase, attrs={'class': 'u-url'}, user=user)
            if content:
                obj.content = content
                obj.url = url

    return objects, new_before, new_after


def fetch_page(query, model_class, by=None):
    """Fetches a page of results from a datastore query.

    Uses the ``before`` and ``after`` query params (if provided; should be
    ISO8601 timestamps) and the ``by`` property to identify the page to fetch.

    Populates a ``log_url_path`` property on each result entity that points to a
    its most recent logged request.

    Args:
      query (google.cloud.ndb.query.Query)
      model_class (class)
      by (ndb.model.Property): paging property, eg :attr:`Object.updated`
        or :attr:`Object.created`

    Returns:
      (list of Object or Follower, str, str) tuple: (results, new_before,
      new_after), where new_before and new_after are query param values for
      ``before`` and ``after`` to fetch the previous and next pages,
      respectively
    """
    assert by

    # if there's a paging param ('before' or 'after'), update query with it
    # TODO: unify this with Bridgy's user page
    def get_paging_param(param):
        val = request.values.get(param)
        if val:
            try:
                dt = util.parse_iso8601(val.replace(' ', '+'))
            except BaseException as e:
                error(f"Couldn't parse {param}, {val!r} as ISO8601: {e}")
            if dt.tzinfo:
                dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
            return dt

    before = get_paging_param('before')
    after = get_paging_param('after')
    if before and after:
        error("can't handle both before and after")
    elif after:
        query = query.filter(by >= after).order(by)
    elif before:
        query = query.filter(by < before).order(-by)
    else:
        query = query.order(-by)

    query_iter = query.iter()
    results = sorted(itertools.islice(query_iter, 0, PAGE_SIZE),
                     key=lambda r: r.updated, reverse=True)

    # calculate new paging param(s)
    has_next = results and query_iter.probably_has_next()
    new_after = (
        before if before
        else results[0].updated if has_next and after
        else None)
    if new_after:
        new_after = new_after.isoformat()

    new_before = (
        after if after else
        results[-1].updated if has_next
        else None)
    if new_before:
        new_before = new_before.isoformat()

    return results, new_before, new_after


def get_original(copy_id, keys_only=None):
    """Fetches a user or object with a given id in copies.

    Thin wrapper around :func:`get_copies` that returns the first
    matching result.

    Also see :Object:`get_copy` and :User:`get_copy`.

    Args:
      copy_id (str)
      keys_only (bool): passed through to :class:`google.cloud.ndb.Query`

    Returns:
      User or Object:
    """
    got = get_originals([copy_id], keys_only=keys_only)
    if got:
        return got[0]


def get_originals(copy_ids, keys_only=None):
    """Fetches users (across all protocols) for a given set of copies.

    Also see :Object:`get_copy` and :User:`get_copy`.

    Args:
      copy_ids (sequence of str)
      keys_only (bool): passed through to :class:`google.cloud.ndb.Query`

    Returns:
      sequence of User and/or Object
    """
    assert copy_ids

    classes = set(cls for cls in PROTOCOLS.values() if cls)
    classes.add(Object)

    return list(itertools.chain(*(
        cls.query(cls.copies.uri.IN(copy_ids)).iter(keys_only=keys_only)
        for cls in classes)))

    # TODO: default to looking up copy_ids as key ids, across protocols? is
    # that useful anywhere?
