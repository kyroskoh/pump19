#!/usr/bin/env python3
# vim:fileencoding=utf-8:ts=8:et:sw=4:sts=4:tw=79

"""
command.py

Handle commands received on IRC.

Copyright (c) 2015 Twisted Pear <tp at pump19 dot eu>
See the file LICENSE for copying permission.
"""

import aiohttp
import aiomc
import aiomumble
import asyncio
import dbutils
import functools
import locale
import logging
import random
import re
import songs
import twitch

CODEFALL_URL = "https://pump19.eu/codefall"
COMMAND_URL = "https://pump19.eu/commands"
PATREON_URL = "https://www.patreon.com/loadingreadyrun"
PATREON_API = "https://api.patreon.com/campaigns/112364"

CMD_REGEX = {
    "patreon":
        re.compile("^patreon$"),
    "latest":
        re.compile("^latest"
                   "(?: (?P<feed>video|podcast|broadcast|highlight))?$"),
    "codefall":
        re.compile("^codefall(?: (?P<limit>\d))?$"),
    "lrrmc":
        re.compile("^lrrmc$"),
    "lrrftb":
        re.compile("^lrrftb$"),
    "mumble":
        re.compile("^mumble$"),
    "lastfm":
        re.compile("^last\.fm (?P<user>\w+)$", re.ASCII),
    "song":
        re.compile("^song$"),
    "help":
        re.compile("^help$")
}

# set up locale for currency formatting (patreon command wants that)
locale.setlocale(locale.LC_MONETARY, "en_US.utf8")


class CommandHandler:
    """
    The command handler interacts with an IRC client and dispatches commands.
    It registers itself as a handler for PRIVMSG events.
    """
    logger = logging.getLogger("command")

    class Limiter:
        """
        A decorator that suppresses method calls within a certain time span.
        """

        logger = logging.getLogger("command.limiter")

        def __init__(self, *, span=15, loop=None):
            """Initialize rate limiter with a default delay of 30."""
            self.span = span
            self.loop = loop or asyncio.get_event_loop()

        def __call__(self, func):

            @functools.wraps(func)
            @asyncio.coroutine
            def wrapper(*args, **kwargs):
                now = self.loop.time()
                if (now - wrapper._spam_last) > wrapper._spam_span:
                    wrapper._spam_last = now
                    yield from func(*args, **kwargs)
                else:
                    self.logger.warning("Suppressed call to {name}.".format(
                        name=func.__name__))

            # each wrapper remembers its own delay and last call
            wrapper._spam_span = self.span
            wrapper._spam_last = 0.0

            return wrapper

    rate_limited = Limiter()

    class CommandRouter:
        """A simple router matching strings against a set of regular
           expressions match to a callable."""

        routes = list()

        def add_route(self, regex, callback):
            self.routes.append((regex, callback))

        def get_route(self, string):
            for (regex, callback) in self.routes:
                match = regex.fullmatch(string)
                if not match:
                    continue

                # add matching groups to function
                return functools.partial(callback, **match.groupdict())

            return None

    router = CommandRouter()

    def __init__(self, client, feed, *, prefix="&", override=None):
        """Initialize the command handler and register for PRIVMSG events."""
        self.logger.info("Creating CommandHandler instance.")

        self.prefix = prefix
        self.override = override
        self.client = client
        self.feed = feed
        self.client.event_handler("PRIVMSG")(self.handle_privmsg)

        self.setup_routing()

    def setup_routing(self):
        """Connect command handlers to regular expressions using the router."""
        for key, regex in CMD_REGEX.items():
            cmd_name = "handle_command_{0}".format(key)
            handle_command = getattr(self, cmd_name, None)
            if handle_command and callable(handle_command):
                self.router.add_route(regex, handle_command)

    @asyncio.coroutine
    def handle_privmsg(self, nick, target, message):
        """
        Handle a PRIVMSG event and dispatch any command to the relevant method.
        """
        # ignore everything that's not a command with our prefix
        if not message.startswith(self.prefix) or len(message) < 2:
            return

        # remove prefix, regex will retrieve the arguments
        cmd = message[1:]

        self.logger.info("Got command \"{0}\" from {1}.".format(cmd, nick))

        # is this a query? if so, send messages to nick instead
        if target == self.client.nickname:
            target = nick

        # check if we can handle that command
        handle_command = self.router.get_route(cmd)
        if handle_command and callable(handle_command):
            yield from handle_command(target, nick)

    @rate_limited
    @asyncio.coroutine
    def handle_command_patreon(self, target, nick):
        """
        Handle !patreon command.
        Post the number of patrons and the total earnings per month.
        """
        patreon_req = yield from aiohttp.request("get", PATREON_API)
        patreon_dict = yield from patreon_req.json()
        patreon_data = patreon_dict.get("data")
        if not patreon_data:
            no_patreon_msg = ("Could not retrieve Patreon data. Check out the "
                              "campaign at {0}").format(PATREON_URL)
            yield from self.client.privmsg(target, no_patreon_msg)
            return

        nof_patrons = patreon_data.get("patron_count", "N/A")
        pledge_sum = patreon_data.get("pledge_sum")
        if pledge_sum:
            pledge_sum = int(pledge_sum) / 100  # stored in cents
            total_earnings = locale.currency(pledge_sum, grouping=True)
        else:
            total_earnings = "N/A"

        patreon_msg = ("{0} patrons for a total of {1} per month. "
                       "{2}".format(nof_patrons, total_earnings, PATREON_URL))

        yield from self.client.privmsg(target, patreon_msg)

    @rate_limited
    @asyncio.coroutine
    def handle_command_latest(self, target, nick, *, feed=None):
        """
        Handle !latest [video|podcast|broadcast|highlight] command.
        Post the most recent RSS feed item or Twitch.tv broadcast.
        """
        feed = feed or "video"

        # broadcasts are updated here
        if feed == "broadcast":
            broadcast = yield from twitch.get_broadcasts("loadingreadyrun", 1)
            video = next(broadcast, None)

            broadcast_msg = "Latest Broadcast: {0} ({1}) [{2}]".format(*video)

            yield from self.client.privmsg(target, broadcast_msg)
        elif feed == "highlight":
            highlight = yield from twitch.get_highlights("loadingreadyrun", 1)
            video = next(highlight, None)

            highlight_msg = "Latest Highlight: {0} ({1}) [{2}]".format(*video)

            yield from self.client.privmsg(target, highlight_msg)
        else:
            # start a manual update
            yield from self.feed.update(feed)

            # let the feed parser announce it
            yield from self.feed.announce(feed, target=target)

    @rate_limited
    @asyncio.coroutine
    def handle_command_codefall(self, target, nick, *, limit=None):
        """
        Handle !codefall [limit] command.
        If available, post a single unclaimed codefall URL.
        """
        limit = min(int(limit), 3) if limit else 1

        entries = yield from dbutils.get_codefall_entries(nick, limit)

        if not entries:
            no_codefall_msg = ("Could not find any unclaimed codes. "
                               "You can add new entries at {url}.".format(
                                   url=CODEFALL_URL))
            yield from self.client.privmsg(target, no_codefall_msg)
            return

        entry_msgs = ("{1} ({2}) {0}".format(*entry) for entry in entries)
        codefall_msg = "Codefall | {0}".format(" | ".join(entry_msgs))

        yield from self.client.privmsg(target, codefall_msg)

    @rate_limited
    @asyncio.coroutine
    def handle_command_lrrmc(self, target, nick):
        """
        Handle !lrrmc command.
        Query and post the status of the LRR Minecraft server.
        """
        # don't stall forever when querying status
        status_coro = aiomc.get_status("rift.dahou.se", 25575)
        try:
            status = yield from asyncio.wait_for(status_coro, 2.0)
        except asyncio.TimeoutError:
            status = None

        base_msg = ("Join the LRR Minecraft Server on lrrmc.rebellious.uno! "
                    "Check http://lrrmap.rebellious.uno/ for the dynamic map. "
                    "Current Status: {status}")

        if not status:
            no_lrrmc_msg = base_msg.format(status="Unknown")
            yield from self.client.privmsg(target, no_lrrmc_msg)
            return

        try:
            nowp = status["players"]["online"]
            maxp = status["players"]["max"]
        except (KeyError, TypeError):
            nowp = maxp = "?"

        status_msg = "Online - {now}/{max} players".format(now=nowp, max=maxp)

        lrrmc_msg = base_msg.format(status=status_msg)
        yield from self.client.privmsg(target, lrrmc_msg)

    @rate_limited
    @asyncio.coroutine
    def handle_command_lrrftb(self, target, nick):
        """
        Handle !lrrftb command.
        Query and post the status of the modded Minecraft server.
        """
        # don't stall forever when querying status
        status_coro = aiomc.get_status("107.172.159.23", 25565)
        try:
            status = yield from asyncio.wait_for(status_coro, 2.0)
        except asyncio.TimeoutError:
            status = None

        base_msg = ("Join the modded Minecraft Server on ftb.lrrcraft.com! "
                    "We're playing Feed The Beej Infinity Evolved v2.3.5. "
                    "Current Status: {status}")

        if not status:
            no_lrrmc_msg = base_msg.format(status="Unknown")
            yield from self.client.privmsg(target, no_lrrmc_msg)
            return

        try:
            nowp = status["players"]["online"]
            maxp = status["players"]["max"]
        except (KeyError, TypeError):
            nowp = maxp = "?"

        status_msg = "Online - {now}/{max} players".format(now=nowp, max=maxp)

        lrrmc_msg = base_msg.format(status=status_msg)
        yield from self.client.privmsg(target, lrrmc_msg)

    @rate_limited
    @asyncio.coroutine
    def handle_command_mumble(self, target, nick):
        """
        Handle !mumble command.
        Query and post the status of the LRR Mumble server.
        """
        # don't stall forever when querying status
        status_coro = aiomumble.get_status("lrr.dahou.se", 64738)
        try:
            status = yield from asyncio.wait_for(status_coro, 2.0)
        except asyncio.TimeoutError:
            status = None

        base_msg = ("Join the LRR Mumble Server on lrr.dahou.se:64738! "
                    "Current Status: {status}")

        if not status:
            no_mumble_msg = base_msg.format(status="Unknown")
            yield from self.client.privmsg(target, no_mumble_msg)
            return

        nowu = status.get("current", 0)
        maxu = status.get("max", 0)
        status_msg = "Online - {now}/{max} users".format(now=nowu, max=maxu)

        mumble_msg = base_msg.format(status=status_msg)
        yield from self.client.privmsg(target, mumble_msg)

    @rate_limited
    @asyncio.coroutine
    def handle_command_lastfm(self, target, nick, *, user=None):
        """
        Handle !last.fm command.
        Query information on the provided last.fm user handle and print the
        most recently listened track.
        """
        coro = songs.get_lastfm_info(user)

        info = yield from coro
        if not info:
            no_lastfm_msg = ("Cannot query last.fm user information for "
                             "{user}.".format(user=user))
            yield from self.client.privmsg(target, no_lastfm_msg)
            return

        name = info.get("name")
        live = info.get("live")
        track = info.get("track")
        artist = info.get("artist")

        if not track and not artist:
            no_lastfm_msg = ("Cannot query most recently played track for "
                             "{name}.".format(name=name))
            yield from self.client.privmsg(target, no_lastfm_msg)
            return

        tempus = "is listening" if live else "last listened"

        lastfm_msg = "{name} {tempus} to \"{track}\" by {artist}".format(
                name=name, tempus=tempus, track=track, artist=artist)
        yield from self.client.privmsg(target, lastfm_msg)

    @rate_limited
    @asyncio.coroutine
    def handle_command_song(self, target, nick):
        """
        Handle !song command.
        This tries to determine the current streamer and prints their most
        recently listened song.
        """
        streamer = yield from twitch.get_streamer()
        if streamer is twitch.Streamers.Offline:
            no_song_msg = "The stream is offline. Please try again later."
            yield from self.client.privmsg(target, no_song_msg)
            return

        # if we found more than one streamer, get a random one
        if isinstance(streamer, list):
            streamer = random.choice(streamer)

        if streamer is twitch.Streamers.Unknown:
            no_song_msg = ("I cannot determine the current streamer. Better "
                           "luck next time.")
            yield from self.client.privmsg(target, no_song_msg)
        elif streamer is twitch.Streamers.Ian:
            yield from self.handle_command_lastfm(
                    target, nick, user="ihorner")
        else:
            no_song_msg = ("I don't know how to stalk {name} yet. Encourage "
                           "them to use last.fm or a similar service.".format(
                               name=streamer.name))
            yield from self.client.privmsg(target, no_song_msg)

    @rate_limited
    @asyncio.coroutine
    def handle_command_help(self, target, nick):
        """
        Handle !help command.
        Posts a link to the golem's list of supported commands.
        """
        help_msg = ("Pump19 is run by Twisted Pear. "
                    "Check {url} for a list of supported commands.").format(
                        url=COMMAND_URL)
        yield from self.client.privmsg(target, help_msg)
