"""Opts a user out and deletes their bridged profiles in other networks.

https://github.com/snarfed/bridgy-fed/issues/783

Usage: opt_out.py [PROTOCOL] [USER_ID] [EXTRA_TARGETS ...]

PROTOCOL: protocol label, eg web, activitypub, atproto
USER_ID: key id of the user entity
EXTRA_TARGETS: bridged profiles will also be deleted here. currently AP only!

Run with:

source local/bin/activate.csh
env PYTHONPATH=. GOOGLE_APPLICATION_CREDENTIALS=service_account_creds.json APPVIEW_HOST=bsky.social PLC_HOST=plc.directory BGS_HOST=bsky.network \
  python scripts/opt_out.py ...
"""
import logging
import sys

from google.cloud import ndb
from oauth_dropins.webutil import appengine_info
appengine_info.DEBUG = False
from oauth_dropins.webutil import appengine_config, flask_util, util

import ids
from models import Object, Target
import protocol
from activitypub import ActivityPub
from web import Web
from app import app

appengine_config.error_reporting_client.host = 'localhost:9999'
appengine_config.error_reporting_client.secure = False

# logging.basicConfig(level=logging.DEBUG)


# Includes top 20-40 each from fedidb.org and fediverse.observer on 2024-01-23
AP_BASE_TARGETS = [
    # Diaspora
    # 'https://joindiaspora.com/',
    # 'https://diasp.org/',

    # Friendica
    'https://venera.social/inbox',

    # kbin (not sharedInbox)
    # dead!
    # 'https://kbin.social/i/inbox',

    # Lemmy (not sharedInbox); these tend to fail on BF objects right now
    'https://alien.top/inbox',
    # 'https://enterprise.lemmy.ml/inbox',
    'https://lemmy.ml/inbox',
    'https://lemmy.world/inbox',
    'https://pasta.faith/inbox',
    'https://r-sauna.fi/inbox',

    # Mastodon
    'https://baraag.net/inbox',
    'https://c.im/inbox',
    # down as of 2024-08-03
    # 'https://daystorm.netz.org/inbox',
    'https://fosstodon.org/inbox',
    # down as of 2024-08-03
    # 'https://gc2.jp/inbox',
    'https://hachyderm.io/inbox',
    'https://indieweb.social/inbox',
    'https://infosec.exchange/inbox',
    'https://mas.to/inbox',
    'https://masto.ai/inbox',
    'https://mastodon.cloud/inbox',
    'https://mastodon.online/inbox',
    'https://mastodon.sdf.org/inbox',
    'https://mastodon.social/inbox',
    'https://mastodon.top/inbox',
    'https://mastodon.uno/inbox',
    'https://mastodon.world/inbox',
    'https://mastodonapp.uk/inbox',
    'https://mozilla.social/inbox',
    'https://mstdn.jp/inbox',
    'https://mstdn.social/inbox',
    'https://pawoo.net/inbox',
    'https://pravda.me/inbox',
    'https://techhub.social/inbox',
    'https://universeodon.com/inbox',

    # Misskey
    'https://misskey.io/inbox',

    # micro.blog
    'https://micro.blog/activitypub/shared/inbox',

    # PixelFed (not sharedInbox)
    'https://pixelfed.social/i/actor/inbox',

    # Twitter bridge
    # 'https://bird.makeup/inbox',
]


def run():
    assert len(sys.argv) >= 3
    proto, user_id = sys.argv[1], sys.argv[2]

    from_proto = protocol.PROTOCOLS[proto]
    kind = from_proto._get_kind()

    if proto == 'activitypub' and user_id.count('@') == 1:
        instance, user = user_id.strip().removeprefix('https://').split('/@')
        user_id = f'@{user}@{instance}'
        print(f'Cleaned up user id to {user_id}')

    if (from_proto.owns_id(user_id) is False
            and from_proto.owns_handle(user_id) is not False):
        handle = user_id
        user_id = from_proto.handle_to_id(handle)
        print(f'Converted {proto} handle {handle} to user id {user_id}')
        assert from_proto.owns_id(user_id) is not False

    user = from_proto.get_by_id(user_id, allow_opt_out=True)

    if not user:
        print(f"user {kind} {user_id} doesn't exist. Creating new and marking as opted out.")
        from_proto(id=user_id, manual_opt_out=True).put()
        return

    if user.manual_opt_out:
        # needed for key_for etc in misc downstream code below
        user.manual_opt_out = False
        user.put()

    assert not user.status, user.status

    # give AS1 delete activity to receive
    delete_base_id = user.web_url() if from_proto is Web else user_id
    delete_id = f'{delete_base_id}#bridgy-fed-delete-{util.now().isoformat()}'
    delete_as1 = {
        'objectType': 'activity',
        'verb': 'delete',
        'id': delete_id,
        'actor': user_id,
        'object': user_id,
    }
    obj = Object(id=delete_id, status='new', source_protocol=from_proto.LABEL,
                 our_as1=delete_as1)

    from_proto.receive(obj, authed_as=user_id, internal=True)

    # delete base and extra AP targets
    if from_proto != ActivityPub:
        delete_ap_targets(from_proto=from_proto, user=user, user_id=user_id)

    if not user.manual_opt_out:
        user.manual_opt_out = True
        user.put()


def delete_ap_targets(*, from_proto=None, user=None, user_id=None):
    delete_base_id = user.web_url() if from_proto is Web else user_id
    delete_id = f'{delete_base_id}#bridgy-fed-delete-{util.now().isoformat()}'
    delete_as1 = {
        'objectType': 'activity',
        'verb': 'delete',
        'id': delete_id,
        'actor': user_id,
        'object': user_id,
    }
    obj = Object(id=delete_id, status='new', source_protocol=from_proto.LABEL,
                 our_as1=delete_as1)
    obj.put()

    extra_targets = sys.argv[3:]
    targets = [Target(protocol='activitypub', uri=t)
               for t in AP_BASE_TARGETS + extra_targets]

    obj.undelivered = targets
    obj.put()

    for target in targets:
        assert util.is_web(target.uri), f'Non-URL target: {target.uri}'
        params = {
            'protocol': target.protocol,
            'url': target.uri,
            'obj': obj.key.urlsafe(),
            'user': user.key.urlsafe(),
            'force': 'true',
        }
        with app.test_request_context('/queue/send', base_url='https://fed.brid.gy/',
                                      data=params, headers={
                                          flask_util.CLOUD_TASKS_TASK_HEADER: 'x',
                                      }):
            # in ActivityPub, if the actor is already deleted on this instance,
            # it may return 502 here because it no longer has the actor's public
            # key, so it can't verify the HTTP Sig. (eg Mastodon does this; it
            # uses LD Sigs for its actor deletes instead.)
            #
            # an alternative is to use the instance actor:
            #
            #   activitypub.instance_actor().key.urlsafe()
            #
            # ...which gets accepted, but I'm not sure all implementations
            # accept the instance actor as authorized to delete a different
            # actor.
            protocol.send_task()


with appengine_config.ndb_client.context(), \
     app.test_request_context(base_url='https://fed.brid.gy/'):
    run()
