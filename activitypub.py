"""ActivityPub protocol implementation."""
from base64 import b64encode
from hashlib import sha256
import itertools
import logging
from urllib.parse import quote_plus

from flask import abort, g, request
from google.cloud import ndb
from granary import as1, as2
from httpsig import HeaderVerifier
from httpsig.requests_auth import HTTPSignatureAuth
from httpsig.utils import parse_signature_header
from oauth_dropins.webutil import appengine_info, flask_util, util
from oauth_dropins.webutil.util import fragmentless, json_dumps, json_loads
import requests
from werkzeug.exceptions import BadGateway

from flask_app import app, cache
import common
from common import (
    add,
    CACHE_TIME,
    CONTENT_TYPE_HTML,
    error,
    host_url,
    NoMicroformats,
    redirect_unwrap,
    redirect_wrap,
    TLD_BLOCKLIST,
)
from models import Follower, Object, PROTOCOLS, User
from protocol import Protocol

# TODO: remove this. we only need it to make sure Web is registered in PROTOCOLS
# before the URL route registrations below.
import web

logger = logging.getLogger(__name__)

CONNEG_HEADERS_AS2_HTML = {
    'Accept': f'{as2.CONNEG_HEADERS["Accept"]}, {CONTENT_TYPE_HTML}; q=0.7'
}

HTTP_SIG_HEADERS = ('Date', 'Host', 'Digest', '(request-target)')

_DEFAULT_SIGNATURE_USER = None

def default_signature_user():
    global _DEFAULT_SIGNATURE_USER
    if _DEFAULT_SIGNATURE_USER is None:
        import web
        _DEFAULT_SIGNATURE_USER = web.Web.get_or_create('snarfed.org')
    return _DEFAULT_SIGNATURE_USER


class ActivityPub(User, Protocol):
    """ActivityPub protocol class.

    Key id is AP/AS2 actor id URL. (*Not* fediverse/WebFinger @-@ handle!)
    """
    ABBREV = 'ap'

    @ndb.ComputedProperty
    def readable_id(self):
        """Returns fediverse handle ie WebFinger address, eg '@me@snarfed.org'."""
        return self.ap_address()

    def _pre_put_hook(self):
        """Validate id, require URL, don't allow Bridgy Fed domains.

        TODO: normalize scheme and domain to lower case. Add that to
        webutil.util.UrlCanonicalizer?
        """
        super()._pre_put_hook()
        id = self.key.id()
        assert id
        assert util.is_web(id), f'{id} is not a URL'
        domain = util.domain_from_link(id)
        assert domain, 'missing domain'
        assert util.domain_from_link(id) not in common.DOMAINS, \
            f'{id} is a Bridgy Fed domain'

    def web_url(self):
        """Returns this user's web URL aka web_url, eg 'https://foo.com/'."""
        if self.obj and self.obj.as1:
            url = util.get_url(self.obj.as1)
            if url:
                return url

        return self.ap_actor()

    def ap_address(self):
        """Returns this user's ActivityPub address, eg '@foo.com@foo.com'."""
        if self.obj and self.obj.as1:
            addr = as2.address(self.as2())
            if addr:
                return addr

        return as2.address(self.key.id())

    def ap_actor(self, rest=None):
        """Returns this user's ActivityPub/AS2 actor id URL.

        Eg 'https://fed.brid.gy/foo.com'
        """
        return self.key.id()

    @classmethod
    def owns_id(cls, id):
        """Returns None if id is an http(s) URL, False otherwise.

        All AP ids are http(s) URLs, but not all http(s) URLs are AP ids.

        https://www.w3.org/TR/activitypub/#obj-id
        """
        return None if util.is_web(id) else False

    @classmethod
    def target_for(cls, obj, shared=False):
        """Returns `obj`'s or its author's/actor's inbox, if available."""
        # TODO: we have entities in prod that fail this, eg
        # https://indieweb.social/users/bismark has source_protocol webmention
        # assert obj.source_protocol in (cls.LABEL, cls.ABBREV, 'ui', None), str(obj)

        if not obj.as1:
            return None

        if obj.type not in as1.ACTOR_TYPES:
            for field in 'actor', 'author', 'attributedTo':
                inner_obj = as1.get_object(obj.as1, field)
                inner_id = inner_obj.get('id') or as1.get_url(inner_obj)
                if not inner_id:
                    continue

                # TODO: need a "soft" kwarg for load to suppress errors?
                actor = cls.load(inner_id)
                if actor and actor.as1:
                    target = cls.target_for(actor)
                    if target:
                        logger.info(f'Target for {obj.key} via {inner_id} is {target}')
                        return target

            logger.info(f'{obj.key} type {obj.type} is not an actor and has no author or actor with inbox')

        actor = obj.as_as2()

        if shared:
            shared_inbox = actor.get('endpoints', {}).get('sharedInbox')
            if shared_inbox:
                return shared_inbox

        return actor.get('publicInbox') or actor.get('inbox')

    @classmethod
    def send(cls, obj, url, log_data=True):
        """Delivers an activity to an inbox URL.

        If `obj.recipient_obj` is set, it's interpreted as the receiving actor
        who we're delivering to and its id is populated into `cc`.
        """
        # this is set in web.webmention_task()
        orig_obj = getattr(obj, 'orig_obj', None)
        orig_as2 = orig_obj.as_as2() if orig_obj else None
        activity = obj.as2 or postprocess_as2(as2.from_as1(obj.as1),
                                              orig_obj=orig_as2)
        activity['actor'] = g.user.ap_actor()
        return signed_post(url, log_data=True, data=activity).ok

    @classmethod
    def fetch(cls, obj, **kwargs):
        """Tries to fetch an AS2 object.

        Assumes obj.id is a URL. Any fragment at the end is stripped before
        loading. This is currently underspecified and somewhat inconsistent
        across AP implementations:

        https://socialhub.activitypub.rocks/t/problems-posting-to-mastodon-inbox/801/11
        https://socialhub.activitypub.rocks/t/problems-posting-to-mastodon-inbox/801/23
        https://socialhub.activitypub.rocks/t/s2s-create-activity/1647/5
        https://github.com/mastodon/mastodon/issues/13879 (open!)
        https://github.com/w3c/activitypub/issues/224

        Uses HTTP content negotiation via the Content-Type header. If the url is
        HTML and it has a rel-alternate link with an AS2 content type, fetches and
        returns that URL.

        Includes an HTTP Signature with the request.
        https://w3c.github.io/activitypub/#authorization
        https://tools.ietf.org/html/draft-cavage-http-signatures-07
        https://github.com/mastodon/mastodon/pull/11269

        Mastodon requires this signature if AUTHORIZED_FETCH aka secure mode is on:
        https://docs.joinmastodon.org/admin/config/#authorized_fetch

        Signs the request with the current user's key. If not provided, defaults to
        using @snarfed.org@snarfed.org's key.

        Args:
          obj: :class:`Object` with the id to fetch. Fills data into the as2
            property.
          kwargs: ignored

        Raises:
          :class:`requests.HTTPError`, :class:`werkzeug.exceptions.HTTPException`

          If we raise a werkzeug HTTPException, it will have an additional
          requests_response attribute with the last requests.Response we received.
        """
        resp = None

        def _error(extra_msg=None):
            msg = f"Couldn't fetch {obj.key.id()} as ActivityStreams 2"
            if extra_msg:
                msg += ': ' + extra_msg
            logger.warning(msg)
            err = BadGateway(msg)
            err.requests_response = resp
            raise err

        def _get(url, headers):
            """Returns None if we fetched and populated, resp otherwise."""
            nonlocal resp
            resp = signed_get(url, headers=headers, gateway=True)
            if not resp.content:
                _error('empty response')
            elif common.content_type(resp) in as2.CONTENT_TYPES:
                try:
                    return resp.json()
                except requests.JSONDecodeError:
                    _error("Couldn't decode as JSON")

        obj.as2 = _get(obj.key.id(), CONNEG_HEADERS_AS2_HTML)
        if obj.as2:
            return obj

        # look in HTML to find AS2 link
        if common.content_type(resp) != 'text/html':
            _error('no AS2 available')
        parsed = util.parse_html(resp)
        link = parsed.find('link', rel=('alternate', 'self'), type=(
            as2.CONTENT_TYPE, as2.CONTENT_TYPE_LD))
        if not (link and link['href']):
            _error('no AS2 available')

        obj.as2 = _get(link['href'], as2.CONNEG_HEADERS)
        if obj.as2:
            return obj

        _error()

    @classmethod
    def serve(cls, obj):
        """Serves an :class:`Object` as AS2."""
        return (postprocess_as2(as2.from_as1(obj.as1)),
                {'Content-Type': as2.CONTENT_TYPE})

    @classmethod
    def verify_signature(cls, activity):
        """Verifies the current request's HTTP Signature.

        Args:
          activity: dict, AS2 activity

        Logs details of the result. Raises :class:`werkzeug.HTTPError` if the
        signature is missing or invalid, otherwise does nothing and returns None.
        """
        headers = dict(request.headers)  # copy so we can modify below
        sig = headers.get('Signature')
        if not sig:
            if appengine_info.DEBUG:
                logging.info('No HTTP Signature, allowing due to DEBUG=true')
                return
            error('No HTTP Signature', status=401)

        logger.info('Verifying HTTP Signature')
        logger.info(f'Headers: {json_dumps(headers, indent=2)}')

        # parse_signature_header lower-cases all keys
        sig_fields = parse_signature_header(sig)
        keyId = fragmentless(sig_fields.get('keyid'))
        if not keyId:
            error('HTTP Signature missing keyId', status=401)

        # TODO: right now, assume hs2019 is rsa-sha256. the real answer is...
        # ...complicated and unclear. 🤷
        # https://github.com/snarfed/bridgy-fed/issues/430#issuecomment-1510462267
        # https://arewehs2019yet.vpzom.click/
        # https://socialhub.activitypub.rocks/t/state-of-http-signatures/754/23
        # https://socialhub.activitypub.rocks/t/http-signatures-libraray/2087/2
        # https://github.com/mastodon/mastodon/pull/14556
        if sig_fields.get('algorithm') == 'hs2019':
            headers['Signature'] = headers['Signature'].replace(
                'algorithm="hs2019"', 'algorithm=rsa-sha256')

        digest = headers.get('Digest') or ''
        if not digest:
            error('Missing Digest header, required for HTTP Signature', status=401)

        expected = b64encode(sha256(request.data).digest()).decode()
        if digest.removeprefix('SHA-256=') != expected:
            error('Invalid Digest header, required for HTTP Signature', status=401)

        try:
            key_actor = cls.load(keyId)
        except BadGateway:
            obj_id = as1.get_object(activity).get('id')
            if (activity.get('type') == 'Delete' and obj_id
                    and keyId == fragmentless(obj_id)):
                logger.info('Object/actor being deleted is also keyId')
                key_actor = Object(id=keyId, source_protocol='activitypub', deleted=True)
                key_actor.put()
            else:
                raise

        if key_actor.deleted:
            abort(202, f'Ignoring, signer {keyId} is already deleted')

        key = key_actor.as2.get("publicKey", {}).get('publicKeyPem')
        logger.info(f'Verifying signature for {request.path} with key {key}')
        try:
            verified = HeaderVerifier(headers, key,
                                      required_headers=['Digest'],
                                      method=request.method,
                                      path=request.path,
                                      sign_header='signature').verify()
        except BaseException as e:
            error(f'HTTP Signature verification failed: {e}', status=401)

        if verified:
            logger.info('HTTP Signature verified!')
        else:
            error('HTTP Signature verification failed', status=401)


def signed_get(url, **kwargs):
    return signed_request(util.requests_get, url, **kwargs)


def signed_post(url, **kwargs):
    assert g.user
    return signed_request(util.requests_post, url, **kwargs)


def signed_request(fn, url, data=None, log_data=True, headers=None, **kwargs):
    """Wraps requests.* and adds HTTP Signature.

    If the current session has a user (ie in g.user), signs with that user's
    key. Otherwise, uses the default user snarfed.org.

    Args:
      fn: :func:`util.requests_get` or  :func:`util.requests_get`
      url: str
      data: optional AS2 object
      log_data: boolean, whether to log full data object
      kwargs: passed through to requests

    Returns: :class:`requests.Response`
    """
    if headers is None:
        headers = {}

    # prepare HTTP Signature and headers
    user = g.user or default_signature_user()

    if data:
        if log_data:
            logger.info(f'Sending AS2 object: {json_dumps(data, indent=2)}')
        data = json_dumps(data).encode()

    headers = {
        **headers,
        # required for HTTP Signature
        # https://tools.ietf.org/html/draft-cavage-http-signatures-07#section-2.1.3
        'Date': util.now().strftime('%a, %d %b %Y %H:%M:%S GMT'),
        # required by Mastodon
        # https://github.com/tootsuite/mastodon/pull/14556#issuecomment-674077648
        'Host': util.domain_from_link(url, minimize=False),
        'Content-Type': as2.CONTENT_TYPE,
        # required for HTTP Signature and Mastodon
        'Digest': f'SHA-256={b64encode(sha256(data or b"").digest()).decode()}',
    }

    logger.info(f"Signing with {user.key}'s key")
    # (request-target) is a special HTTP Signatures header that some fediverse
    # implementations require, eg Peertube.
    # https://datatracker.ietf.org/doc/html/draft-cavage-http-signatures-12#section-2.3
    # https://github.com/snarfed/bridgy-fed/issues/40
    auth = HTTPSignatureAuth(secret=user.private_pem(), key_id=user.ap_actor(),
                             algorithm='rsa-sha256', sign_header='signature',
                             headers=HTTP_SIG_HEADERS)

    # make HTTP request
    kwargs.setdefault('gateway', True)
    resp = fn(url, data=data, auth=auth, headers=headers, allow_redirects=False,
              **kwargs)
    logger.info(f'Got {resp.status_code} headers: {resp.headers}')

    # handle GET redirects manually so that we generate a new HTTP signature
    if resp.is_redirect and fn == util.requests_get:
        return signed_request(fn, resp.headers['Location'], data=data,
                              headers=headers, log_data=log_data, **kwargs)

    type = common.content_type(resp)
    if (type and type != 'text/html' and
        (type.startswith('text/') or type.endswith('+json')
         or type.endswith('/json'))):
        logger.info(resp.text)

    return resp


def postprocess_as2(activity, orig_obj=None, wrap=True):
    """Prepare an AS2 object to be served or sent via ActivityPub.

    g.user is required. Populates it into the actor.id and publicKey fields.

    Args:
      activity: dict, AS2 object or activity
      orig_obj: dict, AS2 object, optional. The target of activity's inReplyTo or
        Like/Announce/etc object, if any.
      wrap: boolean, whether to wrap id, url, object, actor, and attributedTo
    """
    if not activity or isinstance(activity, str):
        return activity

    assert g.user
    type = activity.get('type')

    # actor objects
    if type == 'Person':
        postprocess_as2_actor(activity)
        if g.user and not activity.get('publicKey'):
            # underspecified, inferred from this issue and Mastodon's implementation:
            # https://github.com/w3c/activitypub/issues/203#issuecomment-297553229
            # https://github.com/tootsuite/mastodon/blob/bc2c263504e584e154384ecc2d804aeb1afb1ba3/app/services/activitypub/process_account_service.rb#L77
            actor_url = host_url(activity.get('preferredUsername'))
            activity.update({
                'publicKey': {
                    'id': actor_url,
                    'owner': actor_url,
                    'publicKeyPem': g.user.public_pem().decode(),
                },
                '@context': (util.get_list(activity, '@context') +
                             ['https://w3id.org/security/v1']),
            })
        return activity

    if wrap:
        for field in 'actor', 'attributedTo':
            activity[field] = [postprocess_as2_actor(actor, wrap=wrap)
                               for actor in util.get_list(activity, field)]
            if len(activity[field]) == 1:
                activity[field] = activity[field][0]

    # inReplyTo: singly valued, prefer id over url
    # TODO: ignore orig_obj, do for all inReplyTo
    orig_id = orig_obj.get('id') if orig_obj else None
    in_reply_to = activity.get('inReplyTo')
    if in_reply_to:
        if orig_id:
            activity['inReplyTo'] = orig_id
        elif isinstance(in_reply_to, list):
            if len(in_reply_to) > 1:
                logger.warning(
                    "AS2 doesn't support multiple inReplyTo URLs! "
                    f'Only using the first: {in_reply_to[0]}')
            activity['inReplyTo'] = in_reply_to[0]

        # Mastodon evidently requires a Mention tag for replies to generate a
        # notification to the original post's author. not required for likes,
        # reposts, etc. details:
        # https://github.com/snarfed/bridgy-fed/issues/34
        if orig_obj:
            for to in (util.get_list(orig_obj, 'attributedTo') +
                       util.get_list(orig_obj, 'author') +
                       util.get_list(orig_obj, 'actor')):
                if isinstance(to, dict):
                    to = util.get_first(to, 'url') or to.get('id')
                if to:
                    activity.setdefault('tag', []).append({
                        'type': 'Mention',
                        'href': to,
                    })

    # activity objects (for Like, Announce, etc): prefer id over url
    obj = as1.get_object(activity)
    id = obj.get('id')
    if orig_id and type in as2.TYPES_WITH_OBJECT:
        # inline most objects as bare string ids, not composite objects, for interop
        activity['object'] = orig_id
    elif not id:
        obj['id'] = util.get_first(obj, 'url') or orig_id
    elif g.user and g.user.is_web_url(id):
        obj['id'] = g.user.ap_actor()

    # for Accepts
    if g.user and g.user.is_web_url(as1.get_object(obj).get('id')):
        obj['object'] = g.user.ap_actor()

    # id is required for most things. default to url if it's not set.
    if not activity.get('id'):
        activity['id'] = util.get_first(activity, 'url')

    if wrap:
        # Deletes' object is our own id
        if type == 'Delete':
            activity['object'] = redirect_wrap(activity['object'])
        activity['id'] = redirect_wrap(activity.get('id'))
        activity['url'] = [redirect_wrap(u) for u in util.get_list(activity, 'url')]
        if len(activity['url']) == 1:
            activity['url'] = activity['url'][0]

    # TODO: find a better way to check this, sometimes or always?
    # removed for now since it fires on posts without u-id or u-url, eg
    # https://chrisbeckstrom.com/2018/12/27/32551/
    # assert activity.get('id') or (isinstance(obj, dict) and obj.get('id'))

    # copy image(s) into attachment(s). may be Mastodon-specific.
    # https://github.com/snarfed/bridgy-fed/issues/33#issuecomment-440965618
    obj_or_activity = obj if obj.keys() > set(['id']) else activity
    imgs = util.get_list(obj_or_activity, 'image')
    atts = obj_or_activity.setdefault('attachment', [])
    if imgs:
        atts.extend(img for img in imgs if img not in atts)

    # cc target's author(s) and recipients
    # https://www.w3.org/TR/activitystreams-vocabulary/#audienceTargeting
    # https://w3c.github.io/activitypub/#delivery
    if orig_obj and type in as2.TYPE_TO_VERB:
        recips = itertools.chain(*(util.get_list(orig_obj, field) for field in
                                 ('actor', 'attributedTo', 'to', 'cc')))
        obj_or_activity['cc'] = sorted(util.dedupe_urls(
            util.get_url(recip) or recip.get('id') for recip in recips))

    # to public, since Mastodon interprets to public as public, cc public as unlisted:
    # https://socialhub.activitypub.rocks/t/visibility-to-cc-mapping/284
    # https://wordsmith.social/falkreon/securing-activitypub
    to = activity.setdefault('to', [])
    add(to, as2.PUBLIC_AUDIENCE)

    # hashtags. Mastodon requires:
    # * type: Hashtag
    # * name starts with #
    # * href is set to a valid, fully qualified URL
    #
    # If content has an <a> tag with a fully qualified URL and the hashtag name
    # (with leading #) as its text, Mastodon will rewrite its href to the local
    # instance's search for that hashtag. If content doesn't have a link for a
    # given hashtag, Mastodon won't add one, but that hashtag will still be
    # indexed in search.
    #
    # https://docs.joinmastodon.org/spec/activitypub/#properties-used
    # https://github.com/snarfed/bridgy-fed/issues/45
    for tag in util.get_list(activity, 'tag') + util.get_list(obj, 'tag'):
        name = tag.get('name')
        if name and tag.get('type', 'Tag') == 'Tag':
            tag['type'] = 'Hashtag'
            tag.setdefault('href', common.host_url(
                f'hashtag/{quote_plus(name.removeprefix("#"))}'))
            if not name.startswith('#'):
                tag['name'] = f'#{name}'

    activity['object'] = postprocess_as2(activity.get('object'), orig_obj=orig_obj,
                                         wrap=type in ('Create', 'Update', 'Delete'))

    return util.trim_nulls(activity)


def postprocess_as2_actor(actor, wrap=True):
    """Prepare an AS2 actor object to be served or sent via ActivityPub.

    Modifies actor in place.

    Args:
      actor: dict, AS2 actor object
      wrap: boolean, whether to wrap url

    Returns:
      actor dict
    """
    if not actor:
        return actor
    elif isinstance(actor, str):
        if g.user and g.user.is_web_url(actor):
            return g.user.ap_actor()
        return redirect_wrap(actor)

    url = g.user.web_url() if g.user else None
    urls = util.get_list(actor, 'url')
    if not urls and url:
        urls = [url]

    domain = util.domain_from_link(urls[0], minimize=False)
    if wrap:
        urls[0] = redirect_wrap(urls[0])

    id = actor.get('id')
    if g.user and (not id or g.user.is_web_url(id)):
        actor['id'] = g.user.ap_actor()

    actor.update({
        'url': urls if len(urls) > 1 else urls[0],
        # This has to be the domain for Mastodon interop/Webfinger discovery!
        # See related comment in actor() below.
        'preferredUsername': domain,
    })

    # Override the label for their home page to be "Web site"
    for att in util.get_list(actor, 'attachment'):
        if att.get('type') == 'PropertyValue':
            val = att.get('value', '')
            link = util.parse_html(val).find('a')
            if url and (val == url or link.get('href') == url):
                att['name'] = 'Web site'

    # required by pixelfed. https://github.com/snarfed/bridgy-fed/issues/39
    actor.setdefault('summary', '')
    return actor


@app.get(f'/ap/<any({",".join(PROTOCOLS)}):protocol>/<regex("{common.DOMAIN_RE}"):domain>')
# special case Web users without /ap/web/ prefix, for backward compatibility
@app.get(f'/<regex("{common.DOMAIN_RE}"):domain>', defaults={'protocol': 'web'})
@flask_util.cached(cache, CACHE_TIME)
def actor(protocol, domain):
    """Serves a user's AS2 actor from the datastore."""
    tld = domain.split('.')[-1]
    if tld in TLD_BLOCKLIST:
        error('', status=404)

    cls = PROTOCOLS[protocol]
    g.user = cls.get_or_create(domain)
    if not g.user.obj or not g.user.obj.as1:
        try:
            g.user.obj = cls.load(f'https://{domain}/', gateway=True)
        except NoMicroformats as e:
            pass

    # TODO: unify with common.actor()
    actor = g.user.as2() or {
        '@context': [as2.CONTEXT],
        'type': 'Person',
    }
    actor = postprocess_as2(actor)
    actor.update({
        'id': g.user.ap_actor(),
        # This has to be the domain for Mastodon etc interop! It seems like it
        # should be the custom username from the acct: u-url in their h-card,
        # but that breaks Mastodon's Webfinger discovery. Background:
        # https://docs.joinmastodon.org/spec/activitypub/#properties-used-1
        # https://docs.joinmastodon.org/spec/webfinger/#mastodons-requirements-for-webfinger
        # https://github.com/snarfed/bridgy-fed/issues/302#issuecomment-1324305460
        # https://github.com/snarfed/bridgy-fed/issues/77
        'preferredUsername': domain,
        'inbox': g.user.ap_actor('inbox'),
        'outbox': g.user.ap_actor('outbox'),
        'following': g.user.ap_actor('following'),
        'followers': g.user.ap_actor('followers'),
        'endpoints': {
            'sharedInbox': host_url('/ap/sharedInbox'),
        },
        # add this if we ever change the Web actor ids to be /web/[domain]
        # 'alsoKnownAs': [host_url(domain)],
    })

    logger.info(f'Returning: {json_dumps(actor, indent=2)}')
    return actor, {
        'Content-Type': as2.CONTENT_TYPE,
        'Access-Control-Allow-Origin': '*',
    }


@app.post('/ap/sharedInbox')
@app.post(f'/ap/<any({",".join(PROTOCOLS)}):protocol>/<regex("{common.DOMAIN_RE}"):domain>/inbox')
# special case Web users without /ap/web/ prefix, for backward compatibility
@app.post('/inbox')
@app.post(f'/<regex("{common.DOMAIN_RE}"):domain>/inbox', defaults={'protocol': 'web'})
def inbox(protocol=None, domain=None):
    """Handles ActivityPub inbox delivery."""
    # parse and validate AS2 activity
    try:
        activity = request.json
        assert activity and isinstance(activity, dict)
    except (TypeError, ValueError, AssertionError):
        body = request.get_data(as_text=True)
        error(f"Couldn't parse body as non-empty JSON mapping: {body}", exc_info=True)

    type = activity.get('type')
    actor = as1.get_object(activity, 'actor')
    actor_id = actor.get('id')
    logger.info(f'Got {type} from {actor_id}: {json_dumps(activity, indent=2)}')

    # load user
    if protocol and domain:
        g.user = PROTOCOLS[protocol].get_or_create(domain, direct=False)  # receiving user
        if not g.user.direct and actor_id:
            # this is a deliberate interaction with an indirect receiving user;
            # create a local AP User for the sending user
            actor_obj = ActivityPub.load(actor_id)
            ActivityPub.get_or_create(actor_id, direct=True, obj=actor_obj)

    ActivityPub.verify_signature(activity)

    # check that this activity is public. only do this for creates, not likes,
    # follows, or other activity types, since Mastodon doesn't currently mark
    # those as explicitly public. Use as2's is_public instead of as1's because
    # as1's interprets unlisted as true.
    if type == 'Create' and not as2.is_public(activity):
        logger.info('Dropping non-public activity')
        return 'OK'

    if type == 'Follow':
        # rendered mf2 HTML proxy pages (in render.py) fall back to redirecting
        # to the follow's AS2 id field, but Mastodon's Accept ids are URLs that
        # don't load in browsers, eg:
        # https://jawns.club/ac33c547-ca6b-4351-80d5-d11a6879a7b0
        #
        # so, set a synthetic URL based on the follower's profile.
        # https://github.com/snarfed/bridgy-fed/issues/336
        follower_url = redirect_unwrap(util.get_url(activity, 'actor'))
        followee_url = redirect_unwrap(util.get_url(activity, 'object'))
        activity.setdefault('url', f'{follower_url}#followed-{followee_url}')

    return ActivityPub.receive(activity.get('id'), as2=redirect_unwrap(activity))


@app.get(f'/ap/<any({",".join(PROTOCOLS)}):protocol>/<regex("{common.DOMAIN_RE}"):domain>/<any(followers,following):collection>')
# special case Web users without /ap/web/ prefix, for backward compatibility
@app.get(f'/<regex("{common.DOMAIN_RE}"):domain>/<any(followers,following):collection>',
         defaults={'protocol': 'web'})
@flask_util.cached(cache, CACHE_TIME)
def follower_collection(protocol, domain, collection):
    """ActivityPub Followers and Following collections.

    https://www.w3.org/TR/activitypub/#followers
    https://www.w3.org/TR/activitypub/#collections
    https://www.w3.org/TR/activitystreams-core/#paging
    """
    g.user = PROTOCOLS[protocol].get_by_id(domain)
    if not g.user:
        return f'{protocol} user {domain} not found', 404

    # page
    followers, new_before, new_after = Follower.fetch_page(collection)
    page = {
        'type': 'CollectionPage',
        'partOf': request.base_url,
        'items': util.trim_nulls([f.user.as2() for f in followers]),
    }
    if new_before:
        page['next'] = f'{request.base_url}?before={new_before}'
    if new_after:
        page['prev'] = f'{request.base_url}?after={new_after}'

    if 'before' in request.args or 'after' in request.args:
        page.update({
            '@context': 'https://www.w3.org/ns/activitystreams',
            'id': request.url,
        })
        logger.info(f'Returning {json_dumps(page, indent=2)}')
        return page, {'Content-Type': as2.CONTENT_TYPE}

    # collection
    prop = Follower.to if collection == 'followers' else Follower.from_
    count = Follower.query(
        Follower.status == 'active',
        prop == g.user.key,
    ).count()

    collection = {
        '@context': 'https://www.w3.org/ns/activitystreams',
        'id': request.base_url,
        'type': 'Collection',
        'summary': f"{domain}'s {collection}",
        'totalItems': count,
        'first': page,
    }
    logger.info(f'Returning {json_dumps(collection, indent=2)}')
    return collection, {'Content-Type': as2.CONTENT_TYPE}


@app.get(f'/ap/<any({",".join(PROTOCOLS)}):protocol>/<regex("{common.DOMAIN_RE}"):domain>/outbox')
# special case Web users without /ap/web/ prefix, for backward compatibility
@app.get(f'/<regex("{common.DOMAIN_RE}"):domain>/outbox', defaults={'protocol': 'web'})
def outbox(protocol, domain):
    return {
            '@context': 'https://www.w3.org/ns/activitystreams',
            'id': request.url,
            'summary': f"{domain}'s outbox",
            'type': 'OrderedCollection',
            'totalItems': 0,
            'first': {
                'type': 'CollectionPage',
                'partOf': request.base_url,
                'items': [],
            },
        }, {'Content-Type': as2.CONTENT_TYPE}
