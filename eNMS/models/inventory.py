from flask_login import current_user
from re import search, sub
from sqlalchemy import Boolean, ForeignKey, Integer, or_
from sqlalchemy.ext.associationproxy import association_proxy
from sqlalchemy.orm import backref, deferred, relationship
from sqlalchemy.schema import UniqueConstraint

from eNMS.models.base import AbstractBase
from eNMS.database import db
from eNMS.setup import properties


class Object(AbstractBase):

    __tablename__ = "object"
    type = db.Column(db.SmallString)
    __mapper_args__ = {"polymorphic_identity": "object", "polymorphic_on": type}
    id = db.Column(Integer, primary_key=True)
    last_modified = db.Column(db.SmallString, info={"dont_track_changes": True})
    subtype = db.Column(db.SmallString)
    description = db.Column(db.SmallString)
    model = db.Column(db.SmallString)
    location = db.Column(db.SmallString)
    vendor = db.Column(db.SmallString)

    def update(self, **kwargs):
        super().update(**kwargs)
        if kwargs.get("dont_update_pools", False):
            return
        for pool in db.fetch_all("pool"):
            if pool.manually_defined:
                continue
            match = pool.object_match(self)
            relation, number = f"{self.class_type}s", f"{self.class_type}_number"
            if match and self not in getattr(pool, relation):
                getattr(pool, relation).append(self)
                setattr(pool, number, getattr(pool, number) + 1)
            if self in getattr(pool, relation) and not match:
                getattr(pool, relation).remove(self)
                setattr(pool, number, getattr(pool, number) - 1)

    def delete(self):
        number = f"{self.class_type}_number"
        for pool in self.pools:
            setattr(pool, number, getattr(pool, number) - 1)

    @classmethod
    def rbac_filter(cls, query):
        return query.filter(cls.users.any(id=current_user.id))


@db.set_custom_properties
class Device(Object):

    __tablename__ = class_type = "device"
    __mapper_args__ = {"polymorphic_identity": "device"}
    parent_type = "object"
    id = db.Column(Integer, ForeignKey(Object.id), primary_key=True)
    name = db.Column(db.SmallString, unique=True)
    icon = db.Column(db.SmallString, default="router")
    operating_system = db.Column(db.SmallString)
    os_version = db.Column(db.SmallString)
    ip_address = db.Column(db.SmallString)
    longitude = db.Column(db.SmallString, default="0.0")
    latitude = db.Column(db.SmallString, default="0.0")
    port = db.Column(Integer, default=22)
    username = db.Column(db.SmallString)
    password = db.Column(db.SmallString)
    enable_password = db.Column(db.SmallString)
    netmiko_driver = db.Column(db.SmallString, default="cisco_ios")
    napalm_driver = db.Column(db.SmallString, default="ios")
    configuration = deferred(
        db.Column(db.LargeString, info={"dont_track_changes": True})
    )
    operational_data = deferred(
        db.Column(db.LargeString, info={"dont_track_changes": True})
    )
    last_failure = db.Column(db.SmallString, default="Never")
    last_status = db.Column(db.SmallString, default="Never")
    last_update = db.Column(db.SmallString, default="Never")
    last_runtime = db.Column(db.SmallString)
    last_duration = db.Column(db.SmallString)
    services = relationship(
        "Service", secondary=db.service_device_table, back_populates="devices"
    )
    runs = relationship(
        "Run",
        secondary=db.run_device_table,
        back_populates="devices",
        cascade="all,delete",
    )
    tasks = relationship(
        "Task", secondary=db.task_device_table, back_populates="devices"
    )
    users = relationship(
        "User", secondary=db.user_device_table, back_populates="devices"
    )
    pools = relationship(
        "Pool", secondary=db.pool_device_table, back_populates="devices"
    )
    sessions = relationship(
        "Session", back_populates="device", cascade="all, delete-orphan"
    )

    def table_properties(self, **kwargs):
        columns = [c["data"] for c in kwargs["columns"]]
        rest_api_request = kwargs.get("rest_api_request")
        include_properties = columns if rest_api_request else None
        properties = super().get_properties(include=include_properties)
        context = int(kwargs["form"].get("context-lines", 0))
        for property in ("configuration", "operational_data"):
            if rest_api_request:
                if property in columns:
                    properties[property] = getattr(self, property)
                if f"{property}_matches" not in columns:
                    continue
            data = kwargs["form"].get(property)
            regex_match = kwargs["form"].get(f"{property}_filter") == "regex"
            if not data:
                properties[property] = ""
            else:
                result = []
                content, visited = getattr(self, property).splitlines(), set()
                for (index, line) in enumerate(content):
                    match_lines, merge = [], index - context - 1 in visited
                    if (
                        not search(data, line)
                        if regex_match
                        else data.lower() not in line.lower()
                    ):
                        continue
                    for i in range(-context, context + 1):
                        if index + i < 0 or index + i > len(content) - 1:
                            continue
                        if index + i in visited:
                            merge = True
                            continue
                        visited.add(index + i)
                        line = content[index + i].strip()
                        if rest_api_request:
                            match_lines.append(f"L{index + i + 1}: {line}")
                            continue
                        line = sub(f"(?i){data}", r"<mark>\g<0></mark>", line)
                        match_lines.append(f"<b>L{index + i + 1}:</b> {line}")
                    if rest_api_request:
                        result.extend(match_lines)
                    else:
                        if merge:
                            result[-1] += f"<br>{'<br>'.join(match_lines)}"
                        else:
                            result.append("<br>".join(match_lines))
                if rest_api_request:
                    properties[f"{property}_matches"] = result
                else:
                    properties[property] = "".join(
                        f"<pre style='text-align: left'>{match}</pre>"
                        for match in result
                    )
        return properties

    @property
    def view_properties(self):
        return {
            property: getattr(self, property)
            for property in (
                "id",
                "type",
                "name",
                "icon",
                "latitude",
                "longitude",
                "last_runtime",
            )
        }

    @property
    def ui_name(self):
        return f"{self.name} ({self.model})" if self.model else self.name

    def __repr__(self):
        return f"{self.name} ({self.model})" if self.model else self.name


@db.set_custom_properties
class Link(Object):

    __tablename__ = class_type = "link"
    __mapper_args__ = {"polymorphic_identity": "link"}
    parent_type = "object"
    id = db.Column(Integer, ForeignKey("object.id"), primary_key=True)
    name = db.Column(db.SmallString)
    color = db.Column(db.SmallString, default="#000000")
    source_id = db.Column(Integer, ForeignKey("device.id"))
    destination_id = db.Column(Integer, ForeignKey("device.id"))
    source = relationship(
        Device,
        primaryjoin=source_id == Device.id,
        backref=backref("source", cascade="all, delete-orphan"),
    )
    source_name = association_proxy("source", "name")
    destination = relationship(
        Device,
        primaryjoin=destination_id == Device.id,
        backref=backref("destination", cascade="all, delete-orphan"),
    )
    destination_name = association_proxy("destination", "name")
    pools = relationship("Pool", secondary=db.pool_link_table, back_populates="links")
    users = relationship("User", secondary=db.user_link_table, back_populates="links")
    __table_args__ = (UniqueConstraint(name, source_id, destination_id),)

    def __init__(self, **kwargs):
        self.update(**kwargs)

    @property
    def view_properties(self):
        node_properties = ("id", "longitude", "latitude")
        return {
            **{
                property: getattr(self, property)
                for property in ("id", "type", "name", "color")
            },
            **{
                f"source_{property}": getattr(self.source, property)
                for property in node_properties
            },
            **{
                f"destination_{property}": getattr(self.destination, property)
                for property in node_properties
            },
        }

    def update(self, **kwargs):
        if "source_name" in kwargs:
            kwargs["source"] = db.fetch("device", name=kwargs.pop("source_name")).id
            kwargs["destination"] = db.fetch(
                "device", name=kwargs.pop("destination_name")
            ).id
        kwargs.update(
            {"source_id": kwargs["source"], "destination_id": kwargs["destination"]}
        )
        super().update(**kwargs)


def set_pool_properties(Pool):
    for property in properties["filtering"]["device"]:
        setattr(Pool, f"device_{property}", db.Column(db.LargeString))
        setattr(
            Pool,
            f"device_{property}_match",
            db.Column(db.SmallString, default="inclusion"),
        )
    for property in properties["filtering"]["link"]:
        setattr(Pool, f"link_{property}", db.Column(db.LargeString))
        setattr(
            Pool,
            f"link_{property}_match",
            db.Column(db.SmallString, default="inclusion"),
        )
    return Pool


@set_pool_properties
@db.set_custom_properties
class Pool(AbstractBase):

    __tablename__ = type = "pool"
    id = db.Column(Integer, primary_key=True)
    name = db.Column(db.SmallString, unique=True)
    last_modified = db.Column(db.SmallString, info={"dont_track_changes": True})
    description = db.Column(db.SmallString)
    operator = db.Column(db.SmallString, default="all")
    devices = relationship(
        "Device", secondary=db.pool_device_table, back_populates="pools"
    )
    device_number = db.Column(Integer, default=0)
    links = relationship("Link", secondary=db.pool_link_table, back_populates="pools")
    link_number = db.Column(Integer, default=0)
    latitude = db.Column(db.SmallString, default="0.0")
    longitude = db.Column(db.SmallString, default="0.0")
    services = relationship(
        "Service", secondary=db.service_pool_table, back_populates="pools"
    )
    runs = relationship("Run", secondary=db.run_pool_table, back_populates="pools")
    tasks = relationship("Task", secondary=db.task_pool_table, back_populates="pools")
    groups = relationship(
        "Group", secondary=db.pool_group_table, back_populates="pools"
    )
    manually_defined = db.Column(Boolean, default=False)

    def update(self, **kwargs):
        super().update(**kwargs)
        self.compute_pool()

    def property_match(self, obj, property):
        pool_value = getattr(self, f"{obj.class_type}_{property}")
        object_value = str(getattr(obj, property))
        match = getattr(self, f"{obj.class_type}_{property}_match")
        if not pool_value:
            return True
        elif match == "inclusion":
            return pool_value in object_value
        elif match == "equality":
            return pool_value == object_value
        else:
            return bool(search(pool_value, object_value))

    def object_match(self, obj):
        operator = all if self.operator == "all" else any
        return operator(
            self.property_match(obj, property)
            for property in properties["filtering"][obj.class_type]
        )

    def compute_pool(self):
        if self.manually_defined:
            return
        for object_type in ("device", "link"):
            objects = (
                list(filter(self.object_match, db.fetch_all(object_type)))
                if any(
                    getattr(self, f"{object_type}_{property}")
                    for property in properties["filtering"][object_type]
                )
                else []
            )
            setattr(self, f"{object_type}s", objects)
            setattr(self, f"{object_type}_number", len(objects))
        self.update_rbac()

    @classmethod
    def rbac_filter(cls, query):
        return query.filter(
            or_(cls.groups.any(id=group.id) for group in current_user.groups)
        )

    def update_rbac(self):
        for group in self.groups:
            group.update_rbac()


class Session(AbstractBase):

    __tablename__ = type = "session"
    private = True
    id = db.Column(Integer, primary_key=True)
    name = db.Column(db.SmallString, unique=True)
    timestamp = db.Column(db.SmallString)
    user = db.Column(db.SmallString)
    content = db.Column(db.LargeString, info={"dont_track_changes": True})
    device_id = db.Column(Integer, ForeignKey("device.id"))
    device = relationship(
        "Device", back_populates="sessions", foreign_keys="Session.device_id"
    )
    device_name = association_proxy("device", "name")
