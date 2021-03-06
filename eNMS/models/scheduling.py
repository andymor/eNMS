from re import search
from requests import get, post
from requests.exceptions import ConnectionError, ReadTimeout
from sqlalchemy import Boolean, case, ForeignKey, Integer
from sqlalchemy.ext.associationproxy import association_proxy
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.orm import relationship

from eNMS.database import db
from eNMS.models.base import AbstractBase
from eNMS.setup import scheduler


@db.set_custom_properties
class Task(AbstractBase):

    __tablename__ = type = "task"
    id = db.Column(Integer, primary_key=True)
    name = db.Column(db.SmallString, unique=True)
    description = db.Column(db.SmallString)
    scheduling_mode = db.Column(db.SmallString, default="standard")
    frequency = db.Column(Integer)
    frequency_unit = db.Column(db.SmallString, default="seconds")
    start_date = db.Column(db.SmallString)
    end_date = db.Column(db.SmallString)
    crontab_expression = db.Column(db.SmallString)
    is_active = db.Column(Boolean, default=False)
    initial_payload = db.Column(db.Dict)
    devices = relationship(
        "Device", secondary=db.task_device_table, back_populates="tasks"
    )
    pools = relationship("Pool", secondary=db.task_pool_table, back_populates="tasks")
    service_id = db.Column(Integer, ForeignKey("service.id"))
    service = relationship("Service", back_populates="tasks")
    service_name = association_proxy("service", "name")
    model_properties = ["next_run_time", "time_before_next_run", "status"]

    def __init__(self, **kwargs):
        super().update(**kwargs)

    def update(self, **kwargs):
        super().update(**kwargs)
        if self.is_active:
            self.schedule()

    def delete(self):
        post(f"{scheduler['address']}/delete_job", json=self.id)

    @hybrid_property
    def status(self):
        return "Active" if self.is_active else "Inactive"

    @status.expression
    def status(cls):  # noqa: N805
        return case([(cls.is_active, "Active")], else_="Inactive")

    @property
    def next_run_time(self):
        try:
            return get(
                f"{scheduler['address']}/next_runtime/{self.id}", timeout=0.01
            ).json()
        except (ConnectionError, ReadTimeout):
            return "Scheduler Unreachable"

    @property
    def time_before_next_run(self):
        try:
            return get(
                f"{scheduler['address']}/time_left/{self.id}", timeout=0.01
            ).json()
        except (ConnectionError, ReadTimeout):
            return "Scheduler Unreachable"

    def schedule(self, mode="schedule"):
        try:
            result = post(
                f"{scheduler['address']}/schedule",
                json={"mode": mode, "task": self.get_properties()},
            ).json()
        except ConnectionError:
            return {"alert": "Scheduler Unreachable: the task cannot be scheduled."}
        self.is_active = result.get("active", False)
        return result


@db.set_custom_properties
class Event(AbstractBase):

    __tablename__ = type = "event"
    id = db.Column(Integer, primary_key=True)
    name = db.Column(db.SmallString, unique=True)
    log_source = db.Column(db.SmallString)
    log_source_regex = db.Column(Boolean, default=False)
    log_content = db.Column(db.SmallString)
    log_content_regex = db.Column(Boolean, default=False)
    service_id = db.Column(Integer, ForeignKey("service.id"))
    service = relationship("Service", back_populates="events")
    service_name = association_proxy("service", "name")

    def match_log(self, source, content):
        source_match = (
            search(self.log_source, source)
            if self.log_source_regex
            else self.log_source in source
        )
        content_match = (
            search(self.log_content, content)
            if self.log_content_regex
            else self.log_content in content
        )
        if source_match and content_match:
            self.service.run()
