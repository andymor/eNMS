from passlib.hash import argon2
from copy import deepcopy
from ipaddress import IPv4Network
from json import loads
from logging import info
from os import listdir, makedirs, remove
from os.path import exists, getmtime
from pathlib import Path
from shutil import rmtree
from requests import get as http_get
from ruamel import yaml
from tarfile import open as open_tar
from time import ctime
from traceback import format_exc
from datetime import datetime
from warnings import warn

try:
    from ldap3 import Connection, NTLM, SUBTREE
except ImportError as exc:
    warn(f"Couldn't import ldap3 module ({exc})")

from eNMS.controller.base import BaseController
from eNMS.database import db
from eNMS.models import models
from eNMS.models import relationships


class AdministrationController(BaseController):
    def authenticate_user(self, **kwargs):
        name, password = kwargs["name"], kwargs["password"]
        if not name or not password:
            return False
        user = db.fetch("user", allow_none=True, name=name)
        method = user.authentication if user else kwargs["authentication_method"]
        return getattr(self, f"{method}_authentication")(user, name, password)

    def database_authentication(self, user, name, password):
        hash = self.settings["security"]["hash_user_passwords"]
        verify = argon2.verify if hash else str.__eq__
        user_password = self.get_password(user.password)
        return user if user and verify(password, user_password) else False

    def ldap_authentication(self, user, name, password):
        with Connection(
            self.ldap_client,
            user=f"{self.settings['ldap']['userdn']}\\{name}",
            password=password,
            auto_bind=True,
            authentication=NTLM,
        ) as connection:
            connection.search(
                self.settings["ldap"]["basedn"],
                f"(&(objectClass=person)(samaccountname={name}))",
                search_scope=SUBTREE,
                get_operational_attributes=True,
                attributes=["cn", "memberOf", "mail"],
            )
            response = loads(connection.response_to_json())["entries"][0]
        if not response:
            return
        if not any(
            admin_group in user_group
            for admin_group in self.settings["ldap"]["admin_group"].split(",")
            for user_group in response["attributes"]["memberOf"]
        ):
            return False
        if not user:
            user = db.factory(
                "user",
                authentication="ldap",
                name=name,
                email=response["attributes"].get("mail", ""),
                group="Admin",
            )
            db.session.commit()
        return user

    def tacacs_authentication(self, user, name, password):
        if self.tacacs_client.authenticate(name, password).valid:
            if not user:
                user = db.factory(
                    "user", authentication="tacacs", name=name, group="Admin"
                )
                db.session.commit()
            return user
        else:
            return False

    def database_deletion(self, **kwargs):
        db.delete_all(*kwargs["deletion_types"])

    def result_log_deletion(self, **kwargs):
        date_time_object = datetime.strptime(kwargs["date_time"], "%d/%m/%Y %H:%M:%S")
        date_time_string = date_time_object.strftime("%Y-%m-%d %H:%M:%S.%f")
        for model in kwargs["deletion_types"]:
            if model == "result":
                field_name = "runtime"
            elif model == "changelog":
                field_name = "time"
            session_query = db.session.query(models[model]).filter(
                getattr(models[model], field_name) < date_time_string
            )
            session_query.delete(synchronize_session=False)
            db.session.commit()

    def get_cluster_status(self):
        return [server.status for server in db.fetch_all("server")]

    def get_migration_folders(self):
        return listdir(self.path / "files" / "migrations")

    def objectify(self, model, obj):
        for property, relation in relationships[model].items():
            if property not in obj:
                continue
            elif relation["list"]:
                obj[property] = [
                    db.fetch(relation["model"], name=name).id for name in obj[property]
                ]
            else:
                obj[property] = db.fetch(relation["model"], name=obj[property]).id
        return obj

    def migration_import(self, folder="migrations", **kwargs):
        status, models = "Import successful.", kwargs["import_export_types"]
        skip_update_pools_after_import = kwargs.get(
            "skip_update_pools_after_import", False
        )
        if kwargs.get("empty_database_before_import", False):
            for model in models:
                db.delete_all(model)
                db.session.commit()
        workflow_edges, workflow_services, superworkflows = [], {}, {}
        folder_path = self.path / "files" / folder / kwargs["name"]
        for model in models:
            path = folder_path / f"{model}.yaml"
            if not path.exists():
                continue
            with open(path, "r") as migration_file:
                instances = yaml.load(migration_file)
                if model == "workflow_edge":
                    workflow_edges = deepcopy(instances)
                    continue
                for instance in instances:
                    instance_type = (
                        instance.pop("type") if model == "service" else model
                    )
                    if (
                        instance_type in ("service", "workflow")
                        and "superworkflow" in instance
                    ):
                        superworkflows[instance["name"]] = instance.pop("superworkflow")
                    if instance_type == "workflow":
                        workflow_services[instance["name"]] = instance.pop("services")
                    try:
                        instance = self.objectify(instance_type, instance)
                        db.factory(
                            instance_type, **{"dont_update_pools": True, **instance}
                        )
                        db.session.commit()
                    except Exception:
                        info(
                            f"{str(instance)} could not be imported :"
                            f"{chr(10).join(format_exc().splitlines())}"
                        )
                        status = "Partial import (see logs)."
        try:
            for name, services in workflow_services.items():
                workflow = db.fetch("workflow", name=name)
                workflow.services = [
                    db.fetch("service", name=service_name) for service_name in services
                ]
            db.session.commit()
            for name, superworkflow in superworkflows.items():
                service = db.fetch("service", name=name)
                service.superworkflow = db.fetch("workflow", name=superworkflow)
            db.session.commit()
            for edge in workflow_edges:
                for property in ("source", "destination", "workflow"):
                    edge[property] = db.fetch("service", name=edge[property]).id
                self.update(edge.pop("type"), **edge)
                db.session.commit()
            for service in db.fetch_all("service"):
                service.set_name()
            if not skip_update_pools_after_import:
                for pool in db.fetch_all("pool"):
                    pool.compute_pool()
            self.log("info", status)
        except Exception:
            info(chr(10).join(format_exc().splitlines()))
            status = "Partial import (see logs)."
        return status

    def import_service(self, archive):
        service_name = archive.split(".")[0]
        path = self.path / "files" / "services"
        with open_tar(path / archive) as tar_file:
            tar_file.extractall(path=path)
            status = self.migration_import(
                folder="services",
                name=service_name,
                import_export_types=["service", "workflow_edge"],
            )
        rmtree(path / service_name, ignore_errors=True)
        return status

    def migration_export(self, **kwargs):
        for cls_name in kwargs["import_export_types"]:
            path = self.path / "files" / "migrations" / kwargs["name"]
            if not exists(path):
                makedirs(path)
            with open(path / f"{cls_name}.yaml", "w") as migration_file:
                yaml.dump(db.export(cls_name), migration_file)

    def export_service(self, service_id):
        service = db.fetch("service", id=service_id)
        path = Path(self.path / "files" / "services" / service.filename)
        path.mkdir(parents=True, exist_ok=True)
        services = service.deep_services if service.type == "workflow" else [service]
        services = [service.to_dict(export=True) for service in services]
        for service_dict in services:
            for relation in ("devices", "pools", "events"):
                service_dict.pop(relation)
        with open(path / "service.yaml", "w") as file:
            yaml.dump(services, file)
        if service.type == "workflow":
            with open(path / "workflow_edge.yaml", "w") as file:
                yaml.dump(
                    [edge.to_dict(export=True) for edge in service.deep_edges], file
                )
        with open_tar(f"{path}.tgz", "w:gz") as tar:
            tar.add(path, arcname=service.filename)
        rmtree(path, ignore_errors=True)

    def get_exported_services(self):
        return [f for f in listdir(self.path / "files" / "services") if ".tgz" in f]

    def save_settings(self, **settings):
        self.settings = settings

    def scan_cluster(self, **kwargs):
        protocol = self.settings["cluster"]["scan_protocol"]
        for ip_address in IPv4Network(self.settings["cluster"]["scan_subnet"]):
            try:
                server = http_get(
                    f"{protocol}://{ip_address}/rest/is_alive",
                    timeout=self.settings["cluster"]["scan_timeout"],
                ).json()
                if self.settings["cluster"]["id"] != server.pop("cluster_id"):
                    continue
                db.factory("server", **{**server, **{"ip_address": str(ip_address)}})
            except ConnectionError:
                continue

    def get_tree_files(self, path):
        if path == "root":
            path = self.settings["paths"]["files"] or self.path / "files"
        else:
            path = path.replace(">", "/")
        return [
            {
                "a_attr": {"style": "width: 100%"},
                "data": {
                    "modified": ctime(getmtime(str(file))),
                    "path": str(file),
                    "name": file.name,
                },
                "text": file.name,
                "children": file.is_dir(),
                "type": "folder" if file.is_dir() else "file",
            }
            for file in Path(path).iterdir()
        ]

    def delete_file(self, filepath):
        remove(Path(filepath.replace(">", "/")))

    def edit_file(self, filepath):
        try:
            with open(Path(filepath.replace(">", "/"))) as file:
                return file.read()
        except UnicodeDecodeError:
            return {"error": f"Cannot read file (unsupported type)."}

    def save_file(self, filepath, **kwargs):
        if kwargs.get("file_content"):
            with open(Path(filepath.replace(">", "/")), "w") as file:
                return file.write(kwargs["file_content"])

    def upload_files(self, **kwargs):
        file = kwargs["file"]
        file.save(f"{kwargs['folder']}/{file.filename}")
