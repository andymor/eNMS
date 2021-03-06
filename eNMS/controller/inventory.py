from collections import Counter
from flask_login import current_user
from git import Repo
from io import BytesIO
from logging import info
from sqlalchemy import and_
from subprocess import Popen
from threading import Thread
from uuid import uuid4
from werkzeug.utils import secure_filename
from xlrd import open_workbook
from xlrd.biffh import XLRDError
from xlwt import Workbook


from eNMS.controller.base import BaseController
from eNMS.controller.ssh import SshConnection
from eNMS.database import db
from eNMS.models import models, model_properties, property_types


class InventoryController(BaseController):

    ssh_port = -1

    def get_ssh_port(self):
        self.ssh_port += 1
        start = self.settings["ssh"]["start_port"]
        end = self.settings["ssh"]["end_port"]
        return start + self.ssh_port % (end - start)

    def connection(self, device_id, **kwargs):
        device = db.fetch("device", id=device_id)
        cmd = [str(self.path / "files" / "apps" / "gotty"), "-w"]
        port, protocol = self.get_ssh_port(), kwargs["protocol"]
        address = getattr(device, kwargs["address"])
        cmd.extend(["-p", str(port)])
        if "accept-once" in kwargs:
            cmd.append("--once")
        if "multiplexing" in kwargs:
            cmd.extend(f"tmux new -A -s gotty{port}".split())
        if self.settings["ssh"]["bypass_key_prompt"]:
            options = "-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null"
        else:
            options = ""
        if protocol == "telnet":
            cmd.extend(f"telnet {address}".split())
        elif "authentication" in kwargs:
            login, pwd = (
                (device.username, self.get_password(device.password))
                if kwargs["credentials"] == "device"
                else (current_user.name, self.get_password(current_user.password))
                if kwargs["credentials"] == "user"
                else (kwargs["username"], kwargs["password"])
            )
            cmd.extend(f"sshpass -p {pwd} ssh {options} {login}@{address}".split())
        else:
            cmd.extend(f"ssh {options} {address}".split())
        if protocol != "telnet":
            cmd.extend(f"-p {device.port}".split())
        Popen(cmd)
        return {
            "device": device.name,
            "port": port,
            "redirection": self.settings["ssh"]["port_redirection"],
            "server_addr": self.settings["app"]["address"],
        }

    def get_device_logs(self, device_id):
        device_logs = [
            log.name
            for log in db.fetch_all("log")
            if log.source == db.fetch("device", id=device_id).ip_address
        ]
        return "\n".join(device_logs)

    def handoffssh(self, id, **kwargs):
        device = db.fetch("device", id=id)
        credentials = (
            (device.username, self.get_password(device.password))
            if kwargs["credentials"] == "device"
            else (current_user.name, self.get_password(current_user.password))
            if kwargs["credentials"] == "user"
            else (kwargs["username"], kwargs["password"])
        )
        uuid, port = str(uuid4()), self.get_ssh_port()
        session = db.factory(
            "session",
            name=uuid,
            user=current_user.name,
            timestamp=self.get_time(),
            device=device.id,
        )
        db.session.commit()
        try:
            ssh_connection = SshConnection(
                device.ip_address, *credentials, session.id, uuid, port
            )
            Thread(
                target=ssh_connection.start_session, args=(session.id, uuid, port),
            ).start()
            return {
                "port": port,
                "username": uuid,
                "device_name": device.name,
                "device_ip": device.ip_address,
            }
        except Exception as exc:
            return {"error": exc.args}

    def get_git_history(self, device_id):
        device = db.fetch("device", id=device_id)
        repo = Repo(self.path / "network_data")
        path = self.path / "network_data" / device.name
        return {
            data_type: [
                {"hash": str(commit), "date": commit.committed_datetime}
                for commit in list(repo.iter_commits(paths=path / data_type))
            ]
            for data_type in ("configuration", "operational_data")
        }

    def get_git_network_data(self, device_name, hash):
        tree = Repo(self.path / "network_data").commit(hash).tree
        configuration_file = tree / device_name / "configuration"
        operational_data_file = tree / device_name / "operational_data"
        with BytesIO(configuration_file.data_stream.read()) as f:
            configuration = f.read().decode("utf-8")
        with BytesIO(operational_data_file.data_stream.read()) as f:
            operational_data = f.read().decode("utf-8")
        return {"configuration": configuration, "operational_data": operational_data}

    def get_device_network_data(self, device_id):
        device = db.fetch("device", id=device_id)
        return {
            "configuration": device.configuration,
            "operational_data": device.operational_data,
        }

    def get_session_log(self, session_id):
        return db.fetch("session", id=session_id).content

    def counters(self, property, type):
        return Counter(
            str(getattr(instance, property)) for instance in db.fetch_all(type)
        )

    def export_topology(self, **kwargs):
        workbook = Workbook()
        filename = kwargs["export_filename"]
        if "." not in filename:
            filename += ".xls"
        for obj_type in ("device", "link"):
            sheet = workbook.add_sheet(obj_type)
            for index, property in enumerate(model_properties[obj_type]):
                if property in (
                    "id",
                    "source_id",
                    "destination_id",
                    "configuration",
                    "operational_data",
                ):
                    continue
                sheet.write(0, index, property)
                for obj_index, obj in enumerate(db.fetch_all(obj_type), 1):
                    value = getattr(obj, property)
                    if type(value) == bytes:
                        value = str(self.decrypt(value), "utf-8")
                    sheet.write(obj_index, index, value)
        workbook.save(self.path / "files" / "spreadsheets" / filename)

    def topology_import(self, file):
        book = open_workbook(file_contents=file.read())
        status = "Topology successfully imported."
        for obj_type in ("device", "link"):
            try:
                sheet = book.sheet_by_name(obj_type)
            except XLRDError:
                continue
            properties = sheet.row_values(0)
            for row_index in range(1, sheet.nrows):
                values = {"dont_update_pools": True}
                for index, property in enumerate(properties):
                    if not property:
                        continue
                    func = db.field_conversion[property_types.get(property, "str")]
                    values[property] = func(sheet.row_values(row_index)[index])
                try:
                    db.factory(obj_type, **values).serialized
                except Exception as exc:
                    info(f"{str(values)} could not be imported ({str(exc)})")
                    status = "Partial import (see logs)."
            db.session.commit()
        for pool in db.fetch_all("pool"):
            pool.compute_pool()
        self.log("info", status)
        return status

    def import_topology(self, **kwargs):
        file = kwargs["file"]
        if kwargs["replace"]:
            db.delete_all("device")
            db.session.commit()
        if self.allowed_file(secure_filename(file.filename), {"xls", "xlsx"}):
            result = self.topology_import(file)
        info("Inventory import: Done.")
        return result

    def save_pool_objects(self, pool_id, **kwargs):
        pool = db.fetch("pool", id=pool_id)
        for obj_type in ("device", "link"):
            string_objects = kwargs[f"string_{obj_type}s"]
            if string_objects:
                objects = []
                for name in [obj.strip() for obj in string_objects.split(",")]:
                    obj = db.fetch(obj_type, allow_none=True, name=name)
                    if not obj:
                        return {
                            "alert": f"{obj_type.capitalize()} '{name}' does not exist."
                        }
                    if obj not in objects:
                        objects.append(obj)
            else:
                objects = db.objectify(obj_type, kwargs[f"{obj_type}s"])
            setattr(pool, f"{obj_type}_number", len(objects))
            setattr(pool, f"{obj_type}s", objects)
        pool.update_rbac()
        pool.last_modified = self.get_time()
        return pool.serialized

    def update_pool(self, pool_id):
        db.fetch("pool", id=int(pool_id)).compute_pool()

    def update_all_pools(self):
        for pool in db.fetch_all("pool"):
            pool.compute_pool()

    def get_view_topology(self):
        return {
            "devices": [d.view_properties for d in db.fetch_all("device")],
            "links": [d.view_properties for d in db.fetch_all("link")],
        }

    def view_filtering(self, **kwargs):
        return {
            obj_type: [
                d.view_properties
                for d in db.session.query(models[obj_type])
                .filter(and_(*self.build_filtering_constraints(obj_type, **form)))
                .all()
            ]
            for obj_type, form in kwargs.items()
        }
