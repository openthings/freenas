import os

import iocage.lib.iocage as ioc
import libzfs
from iocage.lib.ioc_check import IOCCheck
from iocage.lib.ioc_clean import IOCClean
from iocage.lib.ioc_fetch import IOCFetch
from iocage.lib.ioc_image import IOCImage
from iocage.lib.ioc_json import IOCJson
# iocage's imports are per command, these are just general facilities
from iocage.lib.ioc_list import IOCList
from iocage.lib.ioc_upgrade import IOCUpgrade
from middlewared.schema import Bool, Dict, List, Str, accepts, Int
from middlewared.service import CRUDService, filterable, job, private
from middlewared.service_exception import CallError
from middlewared.utils import filter_list


class JailService(CRUDService):

    @filterable
    async def query(self, filters=None, options=None):
        options = options or {}
        jails = []
        try:
            jails = [
                list(jail.values())[0]

                for jail in ioc.IOCage().get("all", recursive=True)
            ]
        except BaseException:
            # Brandon is working on fixing this generic except, till then I
            # am not going to make the perfect the enemy of the good enough!
            self.logger.debug("iocage failed to fetch jails", exc_info=True)
            pass

        return filter_list(jails, filters, options)

    @accepts(
        Dict("options",
             Str("release"),
             Str("template"),
             Str("pkglist"),
             Str("uuid"),
             Bool("basejail", default=False),
             Bool("empty", default=False),
             Bool("short", default=False),
             List("props")))
    async def do_create(self, options):
        """Creates a jail."""
        # Typically one would return the created jail's id in this
        # create call BUT since jail creation may or may not involve
        # fetching a release, which in turn could be time consuming
        # and could then block for a long time. This dictates that we
        # make it a job, but that violates the principle that CRUD methods
        # are not jobs as yet, so I settle on making this a wrapper around
        # the main job that calls this and return said job's id instead of
        # the created jail's id

        return await self.middleware.call('jail.create_job', options)

    @private
    @accepts(
        Dict("options",
             Str("release"),
             Str("template"),
             Str("pkglist"),
             Str("uuid"),
             Bool("basejail", default=False),
             Bool("empty", default=False),
             Bool("short", default=False),
             List("props")))
    @job()
    def create_job(self, job, options):
        iocage = ioc.IOCage(skip_jails=True)

        release = options["release"]
        template = options.get("template", False)
        pkglist = options.get("pkglist", None)
        uuid = options.get("uuid", None)
        basejail = options["basejail"]
        empty = options["empty"]
        short = options["short"]
        props = options["props"]
        pool = IOCJson().json_get_value("pool")
        iocroot = IOCJson(pool).json_get_value("iocroot")

        if template:
            release = template

        if not os.path.isdir(f"{iocroot}/releases/{release}") and not \
                template and not empty:
            self.middleware.call_sync('jail.fetch', {"release":
                                                     release}).wait_sync()

        err, msg = iocage.create(
            release,
            props,
            0,
            pkglist,
            template=template,
            short=short,
            _uuid=uuid,
            basejail=basejail,
            empty=empty)

        if err:
            raise CallError(msg)

        return True

    @accepts(Str("jail"), Dict(
             "options",
             Bool("plugin", default=False),
             additional_attrs=True,
             ))
    def do_update(self, jail, options):
        """Sets a jail property."""
        plugin = options.pop("plugin")
        _, _, iocage = self.check_jail_existence(jail)

        name = options.pop("name", None)

        for prop, val in options.items():
            p = f"{prop}={val}"

            iocage.set(p, plugin)

        if name:
            iocage.rename(name)

        return True

    @accepts(Str("jail"))
    def do_delete(self, jail):
        """Takes a jail and destroys it."""
        _, _, iocage = self.check_jail_existence(jail)

        # TODO: Port children checking, release destroying.
        iocage.destroy_jail()

        return True

    @private
    def check_dataset_existence(self):
        IOCCheck()

    @private
    def check_jail_existence(self, jail, skip=True):
        """Wrapper for iocage's API, as a few commands aren't ported to it"""
        try:
            iocage = ioc.IOCage(skip_jails=skip, jail=jail)
            jail, path = iocage.__check_jail_existence__()
        except SystemExit:
            raise CallError(f"jail '{jail}' not found!")

        return jail, path, iocage

    @accepts(
        Dict("options",
             Str("release"),
             Str("server", default="download.freebsd.org"),
             Str("user", default="anonymous"),
             Str("password", default="anonymous@"),
             Str("name", default=None),
             Bool("accept", default=False),
             List("props"),
             List(
                 "files",
                 default=["MANIFEST", "base.txz", "lib32.txz", "doc.txz"])))
    @job(lock=lambda args: f"jail_fetch:{args[-1]}")
    def fetch(self, job, options):
        """Fetches a release or plugin."""
        self.check_dataset_existence()  # Make sure our datasets exist.

        if options["name"] is not None:
            options["plugins"] = True

        iocage = ioc.IOCage()

        iocage.fetch(**options)

        return True

    @accepts(Str("resource", enum=["RELEASE", "TEMPLATE", "PLUGIN"]),
             Bool("remote", default=False))
    def list_resource(self, resource, remote):
        """Returns a JSON list of the supplied resource on the host"""
        self.check_dataset_existence()  # Make sure our datasets exist.
        iocage = ioc.IOCage(skip_jails=True)
        resource = "base" if resource == "RELEASE" else resource.lower()

        if resource == "plugin":
            if remote:
                resource_list = iocage.fetch(list=True, plugins=True,
                                             header=False)
            else:
                resource_list = iocage.list("all", plugin=True)

            for plugin in resource_list:
                for i, elem in enumerate(plugin):
                    # iocage returns - for None
                    plugin[i] = elem if elem != "-" else None
        elif resource == "base":
            resource_list = iocage.fetch(list=True, remote=remote, http=True)
        else:
            resource_list = iocage.list(resource)

        return resource_list

    @accepts(Str("jail"))
    def start(self, jail):
        """Takes a jail and starts it."""
        _, _, iocage = self.check_jail_existence(jail)

        iocage.start()

        return True

    @accepts(Str("jail"))
    def stop(self, jail):
        """Takes a jail and stops it."""
        _, _, iocage = self.check_jail_existence(jail)

        iocage.stop()

        return True

    @accepts(
        Str("jail"),
        Dict(
            "options",
            Str("action", enum=["ADD", "EDIT", "REMOVE", "REPLACE", "LIST"]),
            Str("source"),
            Str("destination"),
            Str("fstype"),
            Str("fsoptions"),
            Str("dump"),
            Str("pass"),
            Int("index", default=None),
        ))
    def fstab(self, jail, options):
        """
        Adds an fstab mount to the jail, mounts if the jail is running.
        """
        _, _, iocage = self.check_jail_existence(jail, skip=False)

        action = options["action"].lower()
        source = options["source"]
        destination = options["destination"]
        fstype = options["fstype"]
        fsoptions = options["fsoptions"]
        dump = options["dump"]
        _pass = options["pass"]
        index = options["index"]

        if action == "replace" and index is None:
            raise ValueError(
                "index must not be None when replacing fstab entry"
            )

        _list = iocage.fstab(action, source, destination, fstype, fsoptions,
                             dump, _pass, index=index)

        if action == "list":
            split_list = {}
            for i in _list:
                split_list[i[0]] = i[1].split()

            return split_list

        return True

    @accepts(Str("pool"))
    def activate(self, pool):
        """Activates a pool for iocage usage, and deactivates the rest."""
        zfs = libzfs.ZFS(history=True, history_prefix="<iocage>")
        pools = zfs.pools
        prop = "org.freebsd.ioc:active"

        for _pool in pools:
            if _pool.name == pool:
                ds = zfs.get_dataset(_pool.name)
                ds.properties[prop] = libzfs.ZFSUserProperty("yes")
            else:
                ds = zfs.get_dataset(_pool.name)
                ds.properties[prop] = libzfs.ZFSUserProperty("no")

        return True

    @accepts(Str("ds_type", enum=["ALL", "JAIL", "TEMPLATE", "RELEASE"]))
    def clean(self, ds_type):
        """Cleans all iocage datasets of ds_type"""

        if ds_type == "JAIL":
            IOCClean().clean_jails()
        elif ds_type == "ALL":
            IOCClean().clean_all()
        elif ds_type == "RELEASE":
            pass
        elif ds_type == "TEMPLATE":
            IOCClean().clean_templates()

        return True

    @accepts(
        Str("jail"),
        List("command"),
        Dict("options", Str("host_user", default="root"), Str("jail_user")))
    def exec(self, jail, command, options):
        """Issues a command inside a jail."""
        _, _, iocage = self.check_jail_existence(jail, skip=False)

        host_user = options["host_user"]
        jail_user = options.get("jail_user", None)

        # We may be getting ';', '&&' and so forth. Adding the shell for
        # safety.

        if len(command) == 1:
            command = ["/bin/sh", "-c"] + command

        host_user = "" if jail_user and host_user == "root" else host_user
        msg = iocage.exec(command, host_user, jail_user, return_msg=True)

        return msg.decode("utf-8")

    @accepts(Str("jail"))
    @job(lock=lambda args: f"jail_update:{args[-1]}")
    def update_to_latest_patch(self, job, jail):
        """Updates specified jail to latest patch level."""

        uuid, path, _ = self.check_jail_existence(jail)
        status, jid = IOCList.list_get_jid(uuid)
        conf = IOCJson(path).json_load()

        # Sometimes if they don't have an existing patch level, this
        # becomes 11.1 instead of 11.1-RELEASE
        _release = conf["release"].rsplit("-", 1)[0]
        release = _release if "-RELEASE" in _release else conf["release"]

        started = False

        if conf["type"] == "jail":
            if not status:
                self.start(jail)
                started = True
        else:
            return False

        if conf["basejail"] != "yes":
            IOCFetch(release).fetch_update(True, uuid)
        else:
            # Basejails only need their base RELEASE updated
            IOCFetch(release).fetch_update()

        if started:
            self.stop(jail)

        return True

    @accepts(Str("jail"), Str("release"))
    @job(lock=lambda args: f"jail_upgrade:{args[-1]}")
    def upgrade(self, job, jail, release):
        """Upgrades specified jail to specified RELEASE."""

        uuid, path, _ = self.check_jail_existence(jail)
        status, jid = IOCList.list_get_jid(uuid)
        conf = IOCJson(path).json_load()
        root_path = f"{path}/root"
        started = False

        if conf["type"] == "jail":
            if not status:
                self.start(jail)
                started = True
        else:
            return False

        IOCUpgrade(conf, release, root_path).upgrade_jail()

        if started:
            self.stop(jail)

        return True

    @accepts(Str("jail"))
    @job(lock=lambda args: f"jail_export:{args[-1]}")
    def export(self, job, jail):
        """Exports jail to zip file"""
        uuid, path, _ = self.check_jail_existence(jail)
        status, jid = IOCList.list_get_jid(uuid)
        started = False

        if status:
            self.stop(jail)
            started = True

        IOCImage().export_jail(uuid, path)

        if started:
            self.start(jail)

        return True

    @accepts(Str("jail"))
    @job(lock=lambda args: f"jail_import:{args[-1]}")
    def _import(self, job, jail):
        """Imports jail from zip file"""

        IOCImage().import_jail(jail)

        return True
