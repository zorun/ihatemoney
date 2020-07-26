import base64
from collections import defaultdict
import datetime
import io
import json
import os
import re
import smtplib
import socket
from time import sleep
import unittest
from unittest.mock import MagicMock, patch

from flask import session
from flask_testing import TestCase
from sqlalchemy import orm
from werkzeug.security import check_password_hash, generate_password_hash

from ihatemoney import history, models, utils
from ihatemoney.currency_convertor import CurrencyConverter
from ihatemoney.manage import DeleteProject, GenerateConfig, GeneratePasswordHash
from ihatemoney.run import create_app, db, load_configuration
from ihatemoney.versioning import LoggingMode

# Unset configuration file env var if previously set
os.environ.pop("IHATEMONEY_SETTINGS_FILE_PATH", None)

__HERE__ = os.path.dirname(os.path.abspath(__file__))


class BaseTestCase(TestCase):

    SECRET_KEY = "TEST SESSION"

    def create_app(self):
        # Pass the test object as a configuration.
        return create_app(self)

    def setUp(self):
        db.create_all()

    def tearDown(self):
        # clean after testing
        db.session.remove()
        db.drop_all()

    def login(self, project, password=None, test_client=None):
        password = password or project

        return self.client.post(
            "/authenticate",
            data=dict(id=project, password=password),
            follow_redirects=True,
        )

    def post_project(self, name, follow_redirects=True):
        """Create a fake project"""
        # create the project
        return self.client.post(
            "/create",
            data={
                "name": name,
                "id": name,
                "password": name,
                "contact_email": f"{name}@notmyidea.org",
                "default_currency": "USD",
            },
            follow_redirects=follow_redirects,
        )

    def create_project(self, name):
        project = models.Project(
            id=name,
            name=str(name),
            password=generate_password_hash(name),
            contact_email=f"{name}@notmyidea.org",
            default_currency="USD",
        )
        models.db.session.add(project)
        models.db.session.commit()


class IhatemoneyTestCase(BaseTestCase):
    SQLALCHEMY_DATABASE_URI = os.environ.get(
        "TESTING_SQLALCHEMY_DATABASE_URI", "sqlite://"
    )
    TESTING = True
    WTF_CSRF_ENABLED = False  # Simplifies the tests.

    def assertStatus(self, expected, resp, url=""):
        return self.assertEqual(
            expected,
            resp.status_code,
            f"{url} expected {expected}, got {resp.status_code}",
        )


class ConfigurationTestCase(BaseTestCase):
    def test_default_configuration(self):
        """Test that default settings are loaded when no other configuration file is specified"""
        self.assertFalse(self.app.config["DEBUG"])
        self.assertEqual(
            self.app.config["SQLALCHEMY_DATABASE_URI"], "sqlite:////tmp/ihatemoney.db"
        )
        self.assertFalse(self.app.config["SQLALCHEMY_TRACK_MODIFICATIONS"])
        self.assertEqual(
            self.app.config["MAIL_DEFAULT_SENDER"],
            ("Budget manager", "budget@notmyidea.org"),
        )

    def test_env_var_configuration_file(self):
        """Test that settings are loaded from a configuration file specified
           with an environment variable."""
        os.environ["IHATEMONEY_SETTINGS_FILE_PATH"] = os.path.join(
            __HERE__, "ihatemoney_envvar.cfg"
        )
        load_configuration(self.app)
        self.assertEqual(self.app.config["SECRET_KEY"], "lalatra")

        # Test that the specified configuration file is loaded
        # even if the default configuration file ihatemoney.cfg exists
        # in the current directory.
        os.environ["IHATEMONEY_SETTINGS_FILE_PATH"] = os.path.join(
            __HERE__, "ihatemoney_envvar.cfg"
        )
        self.app.config.root_path = __HERE__
        load_configuration(self.app)
        self.assertEqual(self.app.config["SECRET_KEY"], "lalatra")

        os.environ.pop("IHATEMONEY_SETTINGS_FILE_PATH", None)

    def test_default_configuration_file(self):
        """Test that settings are loaded from a configuration file if one is found in the
           current directory."""
        self.app.config.root_path = __HERE__
        load_configuration(self.app)
        self.assertEqual(self.app.config["SECRET_KEY"], "supersecret")


class BudgetTestCase(IhatemoneyTestCase):
    def test_notifications(self):
        """Test that the notifications are sent, and that email addresses
        are checked properly.
        """
        # sending a message to one person
        with self.app.mail.record_messages() as outbox:

            # create a project
            self.login("raclette")

            self.post_project("raclette")
            resp = self.client.post(
                "/raclette/invite",
                data={"emails": "zorglub@notmyidea.org"},
                follow_redirects=True,
            )

            # success notification
            self.assertIn("Your invitations have been sent", resp.data.decode("utf-8"))

            self.assertEqual(len(outbox), 2)
            self.assertEqual(outbox[0].recipients, ["raclette@notmyidea.org"])
            self.assertEqual(outbox[1].recipients, ["zorglub@notmyidea.org"])

        # sending a message to multiple persons
        with self.app.mail.record_messages() as outbox:
            self.client.post(
                "/raclette/invite",
                data={"emails": "zorglub@notmyidea.org, toto@notmyidea.org"},
            )

            # only one message is sent to multiple persons
            self.assertEqual(len(outbox), 1)
            self.assertEqual(
                outbox[0].recipients, ["zorglub@notmyidea.org", "toto@notmyidea.org"]
            )

        # mail address checking
        with self.app.mail.record_messages() as outbox:
            response = self.client.post("/raclette/invite", data={"emails": "toto"})
            self.assertEqual(len(outbox), 0)  # no message sent
            self.assertIn("The email toto is not valid", response.data.decode("utf-8"))

        # mixing good and wrong addresses shouldn't send any messages
        with self.app.mail.record_messages() as outbox:
            self.client.post(
                "/raclette/invite", data={"emails": "zorglub@notmyidea.org, zorglub"}
            )  # not valid

            # only one message is sent to multiple persons
            self.assertEqual(len(outbox), 0)

    def test_invite(self):
        """Test that invitation e-mails are sent properly
        """
        self.login("raclette")
        self.post_project("raclette")
        with self.app.mail.record_messages() as outbox:
            self.client.post("/raclette/invite", data={"emails": "toto@notmyidea.org"})
            self.assertEqual(len(outbox), 1)
            url_start = outbox[0].body.find("You can log in using this link: ") + 32
            url_end = outbox[0].body.find(".\n", url_start)
            url = outbox[0].body[url_start:url_end]
        self.client.get("/exit")
        # Test that we got a valid token
        resp = self.client.get(url, follow_redirects=True)
        self.assertIn(
            'You probably want to <a href="/raclette/members/add"',
            resp.data.decode("utf-8"),
        )
        # Test empty and invalid tokens
        self.client.get("/exit")
        resp = self.client.get("/authenticate")
        self.assertIn("You either provided a bad token", resp.data.decode("utf-8"))
        resp = self.client.get("/authenticate?token=token")
        self.assertIn("You either provided a bad token", resp.data.decode("utf-8"))

    def test_password_reminder(self):
        # test that it is possible to have an email containing the password of a
        # project in case people forget it (and it happens!)

        self.create_project("raclette")

        with self.app.mail.record_messages() as outbox:
            # a nonexisting project should not send an email
            self.client.post("/password-reminder", data={"id": "unexisting"})
            self.assertEqual(len(outbox), 0)

            # a mail should be sent when a project exists
            self.client.post("/password-reminder", data={"id": "raclette"})
            self.assertEqual(len(outbox), 1)
            self.assertIn("raclette", outbox[0].body)
            self.assertIn("raclette@notmyidea.org", outbox[0].recipients)

    def test_password_reset(self):
        # test that a password can be changed using a link sent by mail

        self.create_project("raclette")
        # Get password resetting link from mail
        with self.app.mail.record_messages() as outbox:
            resp = self.client.post(
                "/password-reminder", data={"id": "raclette"}, follow_redirects=True
            )
            # Check that we are redirected to the right page
            self.assertIn(
                "A link to reset your password has been sent to you",
                resp.data.decode("utf-8"),
            )
            # Check that an email was sent
            self.assertEqual(len(outbox), 1)
            url_start = outbox[0].body.find("You can reset it here: ") + 23
            url_end = outbox[0].body.find(".\n", url_start)
            url = outbox[0].body[url_start:url_end]
        # Test that we got a valid token
        resp = self.client.get(url)
        self.assertIn("Password confirmation</label>", resp.data.decode("utf-8"))
        # Test that password can be changed
        self.client.post(
            url, data={"password": "pass", "password_confirmation": "pass"}
        )
        resp = self.login("raclette", password="pass")
        self.assertIn(
            "<title>Account manager - raclette</title>", resp.data.decode("utf-8")
        )
        # Test empty and null tokens
        resp = self.client.get("/reset-password")
        self.assertIn("No token provided", resp.data.decode("utf-8"))
        resp = self.client.get("/reset-password?token=token")
        self.assertIn("Invalid token", resp.data.decode("utf-8"))

    def test_project_creation(self):
        with self.app.test_client() as c:

            with self.app.mail.record_messages() as outbox:
                # add a valid project
                resp = c.post(
                    "/create",
                    data={
                        "name": "The fabulous raclette party",
                        "id": "raclette",
                        "password": "party",
                        "contact_email": "raclette@notmyidea.org",
                        "default_currency": "USD",
                    },
                    follow_redirects=True,
                )
                # an email is sent to the owner with a reminder of the password
                self.assertEqual(len(outbox), 1)
                self.assertEqual(outbox[0].recipients, ["raclette@notmyidea.org"])
                self.assertIn(
                    "A reminder email has just been sent to you",
                    resp.data.decode("utf-8"),
                )

            # session is updated
            self.assertTrue(session["raclette"])

            # project is created
            self.assertEqual(len(models.Project.query.all()), 1)

            # Add a second project with the same id
            models.Project.query.get("raclette")

            c.post(
                "/create",
                data={
                    "name": "Another raclette party",
                    "id": "raclette",  # already used !
                    "password": "party",
                    "contact_email": "raclette@notmyidea.org",
                    "default_currency": "USD",
                },
            )

            # no new project added
            self.assertEqual(len(models.Project.query.all()), 1)

    def test_project_creation_without_public_permissions(self):
        self.app.config["ALLOW_PUBLIC_PROJECT_CREATION"] = False
        with self.app.test_client() as c:
            # add a valid project
            c.post(
                "/create",
                data={
                    "name": "The fabulous raclette party",
                    "id": "raclette",
                    "password": "party",
                    "contact_email": "raclette@notmyidea.org",
                    "default_currency": "USD",
                },
            )

            # session is not updated
            self.assertNotIn("raclette", session)

            # project is not created
            self.assertEqual(len(models.Project.query.all()), 0)

    def test_project_creation_with_public_permissions(self):
        self.app.config["ALLOW_PUBLIC_PROJECT_CREATION"] = True
        with self.app.test_client() as c:
            # add a valid project
            c.post(
                "/create",
                data={
                    "name": "The fabulous raclette party",
                    "id": "raclette",
                    "password": "party",
                    "contact_email": "raclette@notmyidea.org",
                    "default_currency": "USD",
                },
            )

            # session is updated
            self.assertTrue(session["raclette"])

            # project is created
            self.assertEqual(len(models.Project.query.all()), 1)

    def test_project_deletion(self):

        with self.app.test_client() as c:
            c.post(
                "/create",
                data={
                    "name": "raclette party",
                    "id": "raclette",
                    "password": "party",
                    "contact_email": "raclette@notmyidea.org",
                    "default_currency": "USD",
                },
            )

            # project added
            self.assertEqual(len(models.Project.query.all()), 1)

            c.get("/raclette/delete")

            # project removed
            self.assertEqual(len(models.Project.query.all()), 0)

    def test_bill_placeholder(self):
        self.post_project("raclette")
        self.login("raclette")

        result = self.client.get("/raclette/")

        # Empty bill list and no members, should now propose to add members first
        self.assertIn(
            'You probably want to <a href="/raclette/members/add"',
            result.data.decode("utf-8"),
        )

        result = self.client.post("/raclette/members/add", data={"name": "zorglub"})

        result = self.client.get("/raclette/")

        # Empty bill with member, list should now propose to add bills
        self.assertIn(
            'You probably want to <a href="/raclette/add"', result.data.decode("utf-8")
        )

    def test_membership(self):
        self.post_project("raclette")
        self.login("raclette")

        # adds a member to this project
        self.client.post("/raclette/members/add", data={"name": "zorglub"})
        self.assertEqual(len(models.Project.query.get("raclette").members), 1)

        # adds him twice
        result = self.client.post("/raclette/members/add", data={"name": "zorglub"})

        # should not accept him
        self.assertEqual(len(models.Project.query.get("raclette").members), 1)

        # add fred
        self.client.post("/raclette/members/add", data={"name": "fred"})
        self.assertEqual(len(models.Project.query.get("raclette").members), 2)

        # check fred is present in the bills page
        result = self.client.get("/raclette/")
        self.assertIn("fred", result.data.decode("utf-8"))

        # remove fred
        self.client.post(
            "/raclette/members/%s/delete"
            % models.Project.query.get("raclette").members[-1].id
        )

        # as fred is not bound to any bill, he is removed
        self.assertEqual(len(models.Project.query.get("raclette").members), 1)

        # add fred again
        self.client.post("/raclette/members/add", data={"name": "fred"})
        fred_id = models.Project.query.get("raclette").members[-1].id

        # bound him to a bill
        result = self.client.post(
            "/raclette/add",
            data={
                "date": "2011-08-10",
                "what": "fromage à raclette",
                "payer": fred_id,
                "payed_for": [fred_id],
                "amount": "25",
            },
        )

        # remove fred
        self.client.post(f"/raclette/members/{fred_id}/delete")

        # he is still in the database, but is deactivated
        self.assertEqual(len(models.Project.query.get("raclette").members), 2)
        self.assertEqual(len(models.Project.query.get("raclette").active_members), 1)

        # as fred is now deactivated, check that he is not listed when adding
        # a bill or displaying the balance
        result = self.client.get("/raclette/")
        self.assertNotIn(
            (f"/raclette/members/{fred_id}/delete"), result.data.decode("utf-8")
        )

        result = self.client.get("/raclette/add")
        self.assertNotIn("fred", result.data.decode("utf-8"))

        # adding him again should reactivate him
        self.client.post("/raclette/members/add", data={"name": "fred"})
        self.assertEqual(len(models.Project.query.get("raclette").active_members), 2)

        # adding an user with the same name as another user from a different
        # project should not cause any troubles
        self.post_project("randomid")
        self.login("randomid")
        self.client.post("/randomid/members/add", data={"name": "fred"})
        self.assertEqual(len(models.Project.query.get("randomid").active_members), 1)

    def test_person_model(self):
        self.post_project("raclette")
        self.login("raclette")

        # adds a member to this project
        self.client.post("/raclette/members/add", data={"name": "zorglub"})
        zorglub = models.Project.query.get("raclette").members[-1]

        # should not have any bills
        self.assertFalse(zorglub.has_bills())

        # bound him to a bill
        self.client.post(
            "/raclette/add",
            data={
                "date": "2011-08-10",
                "what": "fromage à raclette",
                "payer": zorglub.id,
                "payed_for": [zorglub.id],
                "amount": "25",
            },
        )

        # should have a bill now
        zorglub = models.Project.query.get("raclette").members[-1]
        self.assertTrue(zorglub.has_bills())

    def test_member_delete_method(self):
        self.post_project("raclette")
        self.login("raclette")

        # adds a member to this project
        self.client.post("/raclette/members/add", data={"name": "zorglub"})

        # try to remove the member using GET method
        response = self.client.get("/raclette/members/1/delete")
        self.assertEqual(response.status_code, 405)

        # delete user using POST method
        self.client.post("/raclette/members/1/delete")
        self.assertEqual(len(models.Project.query.get("raclette").active_members), 0)
        # try to delete an user already deleted
        self.client.post("/raclette/members/1/delete")

    def test_demo(self):
        # test that a demo project is created if none is defined
        self.assertEqual([], models.Project.query.all())
        self.client.get("/demo")
        demo = models.Project.query.get("demo")
        self.assertTrue(demo is not None)

        self.assertEqual(["Amina", "Georg", "Alice"], [m.name for m in demo.members])
        self.assertEqual(demo.get_bills().count(), 3)

    def test_deactivated_demo(self):
        self.app.config["ACTIVATE_DEMO_PROJECT"] = False

        # test redirection to the create project form when demo is deactivated
        resp = self.client.get("/demo")
        self.assertIn('<a href="/create?project_id=demo">', resp.data.decode("utf-8"))

    def test_authentication(self):
        # try to authenticate without credentials should redirect
        # to the authentication page
        resp = self.client.post("/authenticate")
        self.assertIn("Authentication", resp.data.decode("utf-8"))

        # raclette that the login / logout process works
        self.create_project("raclette")

        # try to see the project while not being authenticated should redirect
        # to the authentication page
        resp = self.client.get("/raclette", follow_redirects=True)
        self.assertIn("Authentication", resp.data.decode("utf-8"))

        # try to connect with wrong credentials should not work
        with self.app.test_client() as c:
            resp = c.post("/authenticate", data={"id": "raclette", "password": "nope"})

            self.assertIn("Authentication", resp.data.decode("utf-8"))
            self.assertNotIn("raclette", session)

        # try to connect with the right credentials should work
        with self.app.test_client() as c:
            resp = c.post(
                "/authenticate", data={"id": "raclette", "password": "raclette"}
            )

            self.assertNotIn("Authentication", resp.data.decode("utf-8"))
            self.assertIn("raclette", session)
            self.assertTrue(session["raclette"])

            # logout should wipe the session out
            c.get("/exit")
            self.assertNotIn("raclette", session)

        # test that with admin credentials, one can access every project
        self.app.config["ADMIN_PASSWORD"] = generate_password_hash("pass")
        with self.app.test_client() as c:
            resp = c.post("/admin?goto=%2Fraclette", data={"admin_password": "pass"})
            self.assertNotIn("Authentication", resp.data.decode("utf-8"))
            self.assertTrue(session["is_admin"])

    def test_admin_authentication(self):
        self.app.config["ADMIN_PASSWORD"] = generate_password_hash("pass")
        # Disable public project creation so we have an admin endpoint to test
        self.app.config["ALLOW_PUBLIC_PROJECT_CREATION"] = False

        # test the redirection to the authentication page when trying to access admin endpoints
        resp = self.client.get("/create")
        self.assertIn('<a href="/admin?goto=%2Fcreate">', resp.data.decode("utf-8"))

        # test right password
        resp = self.client.post(
            "/admin?goto=%2Fcreate", data={"admin_password": "pass"}
        )
        self.assertIn('<a href="/create">/create</a>', resp.data.decode("utf-8"))

        # test wrong password
        resp = self.client.post(
            "/admin?goto=%2Fcreate", data={"admin_password": "wrong"}
        )
        self.assertNotIn('<a href="/create">/create</a>', resp.data.decode("utf-8"))

        # test empty password
        resp = self.client.post("/admin?goto=%2Fcreate", data={"admin_password": ""})
        self.assertNotIn('<a href="/create">/create</a>', resp.data.decode("utf-8"))

    def test_login_throttler(self):
        self.app.config["ADMIN_PASSWORD"] = generate_password_hash("pass")

        # Activate admin login throttling by authenticating 4 times with a wrong passsword
        self.client.post("/admin?goto=%2Fcreate", data={"admin_password": "wrong"})
        self.client.post("/admin?goto=%2Fcreate", data={"admin_password": "wrong"})
        self.client.post("/admin?goto=%2Fcreate", data={"admin_password": "wrong"})
        resp = self.client.post(
            "/admin?goto=%2Fcreate", data={"admin_password": "wrong"}
        )

        self.assertIn(
            "Too many failed login attempts, please retry later.",
            resp.data.decode("utf-8"),
        )
        # Change throttling delay
        import gc

        for obj in gc.get_objects():
            if isinstance(obj, utils.LoginThrottler):
                obj._delay = 0.005
                break
        # Wait for delay to expire and retry logging in
        sleep(1)
        resp = self.client.post(
            "/admin?goto=%2Fcreate", data={"admin_password": "wrong"}
        )
        self.assertNotIn(
            "Too many failed login attempts, please retry later.",
            resp.data.decode("utf-8"),
        )

    def test_manage_bills(self):
        self.post_project("raclette")

        # add two persons
        self.client.post("/raclette/members/add", data={"name": "zorglub"})
        self.client.post("/raclette/members/add", data={"name": "fred"})

        members_ids = [m.id for m in models.Project.query.get("raclette").members]

        # create a bill
        self.client.post(
            "/raclette/add",
            data={
                "date": "2011-08-10",
                "what": "fromage à raclette",
                "payer": members_ids[0],
                "payed_for": members_ids,
                "amount": "25",
            },
        )
        models.Project.query.get("raclette")
        bill = models.Bill.query.one()
        self.assertEqual(bill.amount, 25)

        # edit the bill
        self.client.post(
            f"/raclette/edit/{bill.id}",
            data={
                "date": "2011-08-10",
                "what": "fromage à raclette",
                "payer": members_ids[0],
                "payed_for": members_ids,
                "amount": "10",
            },
        )

        bill = models.Bill.query.one()
        self.assertEqual(bill.amount, 10, "bill edition")

        # delete the bill
        self.client.get(f"/raclette/delete/{bill.id}")
        self.assertEqual(0, len(models.Bill.query.all()), "bill deletion")

        # test balance
        self.client.post(
            "/raclette/add",
            data={
                "date": "2011-08-10",
                "what": "fromage à raclette",
                "payer": members_ids[0],
                "payed_for": members_ids,
                "amount": "19",
            },
        )

        self.client.post(
            "/raclette/add",
            data={
                "date": "2011-08-10",
                "what": "fromage à raclette",
                "payer": members_ids[1],
                "payed_for": members_ids[0],
                "amount": "20",
            },
        )

        self.client.post(
            "/raclette/add",
            data={
                "date": "2011-08-10",
                "what": "fromage à raclette",
                "payer": members_ids[1],
                "payed_for": members_ids,
                "amount": "17",
            },
        )

        balance = models.Project.query.get("raclette").balance
        self.assertEqual(set(balance.values()), set([19.0, -19.0]))

        # Bill with negative amount
        self.client.post(
            "/raclette/add",
            data={
                "date": "2011-08-12",
                "what": "fromage à raclette",
                "payer": members_ids[0],
                "payed_for": members_ids,
                "amount": "-25",
            },
        )
        bill = models.Bill.query.filter(models.Bill.date == "2011-08-12")[0]
        self.assertEqual(bill.amount, -25)

        # add a bill with a comma
        self.client.post(
            "/raclette/add",
            data={
                "date": "2011-08-01",
                "what": "fromage à raclette",
                "payer": members_ids[0],
                "payed_for": members_ids,
                "amount": "25,02",
            },
        )
        bill = models.Bill.query.filter(models.Bill.date == "2011-08-01")[0]
        self.assertEqual(bill.amount, 25.02)

    def test_weighted_balance(self):
        self.post_project("raclette")

        # add two persons
        self.client.post("/raclette/members/add", data={"name": "zorglub"})
        self.client.post(
            "/raclette/members/add", data={"name": "freddy familly", "weight": 4}
        )

        members_ids = [m.id for m in models.Project.query.get("raclette").members]

        # test balance
        self.client.post(
            "/raclette/add",
            data={
                "date": "2011-08-10",
                "what": "fromage à raclette",
                "payer": members_ids[0],
                "payed_for": members_ids,
                "amount": "10",
            },
        )

        self.client.post(
            "/raclette/add",
            data={
                "date": "2011-08-10",
                "what": "pommes de terre",
                "payer": members_ids[1],
                "payed_for": members_ids,
                "amount": "10",
            },
        )

        balance = models.Project.query.get("raclette").balance
        self.assertEqual(set(balance.values()), set([6, -6]))

    def test_trimmed_members(self):
        self.post_project("raclette")

        # Add two times the same person (with a space at the end).
        self.client.post("/raclette/members/add", data={"name": "zorglub"})
        self.client.post("/raclette/members/add", data={"name": "zorglub "})
        members = models.Project.query.get("raclette").members

        self.assertEqual(len(members), 1)

    def test_weighted_members_list(self):
        self.post_project("raclette")

        # add two persons
        self.client.post("/raclette/members/add", data={"name": "zorglub"})
        self.client.post("/raclette/members/add", data={"name": "tata", "weight": 1})

        resp = self.client.get("/raclette/")
        self.assertIn("extra-info", resp.data.decode("utf-8"))

        self.client.post(
            "/raclette/members/add", data={"name": "freddy familly", "weight": 4}
        )

        resp = self.client.get("/raclette/")
        self.assertNotIn("extra-info", resp.data.decode("utf-8"))

    def test_negative_weight(self):
        self.post_project("raclette")

        # Add one user and edit it to have a negative share
        self.client.post("/raclette/members/add", data={"name": "zorglub"})
        resp = self.client.post(
            "/raclette/members/1/edit", data={"name": "zorglub", "weight": -1}
        )

        # An error should be generated, and its weight should still be 1.
        self.assertIn('<p class="alert alert-danger">', resp.data.decode("utf-8"))
        self.assertEqual(len(models.Project.query.get("raclette").members), 1)
        self.assertEqual(models.Project.query.get("raclette").members[0].weight, 1)

    def test_rounding(self):
        self.post_project("raclette")

        # add members
        self.client.post("/raclette/members/add", data={"name": "zorglub"})
        self.client.post("/raclette/members/add", data={"name": "fred"})
        self.client.post("/raclette/members/add", data={"name": "tata"})

        # create bills
        self.client.post(
            "/raclette/add",
            data={
                "date": "2011-08-10",
                "what": "fromage à raclette",
                "payer": 1,
                "payed_for": [1, 2, 3],
                "amount": "24.36",
            },
        )

        self.client.post(
            "/raclette/add",
            data={
                "date": "2011-08-10",
                "what": "red wine",
                "payer": 2,
                "payed_for": [1],
                "amount": "19.12",
            },
        )

        self.client.post(
            "/raclette/add",
            data={
                "date": "2011-08-10",
                "what": "delicatessen",
                "payer": 1,
                "payed_for": [1, 2],
                "amount": "22",
            },
        )

        balance = models.Project.query.get("raclette").balance
        result = {}
        result[models.Project.query.get("raclette").members[0].id] = 8.12
        result[models.Project.query.get("raclette").members[1].id] = 0.0
        result[models.Project.query.get("raclette").members[2].id] = -8.12
        # Since we're using floating point to store currency, we can have some
        # rounding issues that prevent test from working.
        # However, we should obtain the same values as the theoretical ones if we
        # round to 2 decimals, like in the UI.
        for key, value in balance.items():
            self.assertEqual(round(value, 2), result[key])

    def test_edit_project(self):
        # A project should be editable

        self.post_project("raclette")
        new_data = {
            "name": "Super raclette party!",
            "contact_email": "zorglub@notmyidea.org",
            "password": "didoudida",
            "logging_preference": LoggingMode.ENABLED.value,
            "default_currency": "USD",
        }

        resp = self.client.post("/raclette/edit", data=new_data, follow_redirects=True)
        self.assertEqual(resp.status_code, 200)
        project = models.Project.query.get("raclette")

        self.assertEqual(project.name, new_data["name"])
        self.assertEqual(project.contact_email, new_data["contact_email"])
        self.assertEqual(project.default_currency, new_data["default_currency"])
        self.assertTrue(check_password_hash(project.password, new_data["password"]))

        # Editing a project with a wrong email address should fail
        new_data["contact_email"] = "wrong_email"

        resp = self.client.post("/raclette/edit", data=new_data, follow_redirects=True)
        self.assertIn("Invalid email address", resp.data.decode("utf-8"))

    def test_dashboard(self):
        # test that the dashboard is deactivated by default
        resp = self.client.post(
            "/admin?goto=%2Fdashboard",
            data={"admin_password": "adminpass"},
            follow_redirects=True,
        )
        self.assertIn('<div class="alert alert-danger">', resp.data.decode("utf-8"))

        # test access to the dashboard when it is activated
        self.app.config["ACTIVATE_ADMIN_DASHBOARD"] = True
        self.app.config["ADMIN_PASSWORD"] = generate_password_hash("adminpass")
        resp = self.client.post(
            "/admin?goto=%2Fdashboard",
            data={"admin_password": "adminpass"},
            follow_redirects=True,
        )
        self.assertIn(
            "<thead><tr><th>Project</th><th>Number of members",
            resp.data.decode("utf-8"),
        )

    def test_statistics_page(self):
        self.post_project("raclette")
        response = self.client.get("/raclette/statistics")
        self.assertEqual(response.status_code, 200)

    def test_statistics(self):
        self.post_project("raclette")

        # add members
        self.client.post("/raclette/members/add", data={"name": "zorglub", "weight": 2})
        self.client.post("/raclette/members/add", data={"name": "fred"})
        self.client.post("/raclette/members/add", data={"name": "tata"})
        # Add a member with a balance=0 :
        self.client.post("/raclette/members/add", data={"name": "pépé"})

        # create bills
        self.client.post(
            "/raclette/add",
            data={
                "date": "2011-08-10",
                "what": "fromage à raclette",
                "payer": 1,
                "payed_for": [1, 2, 3],
                "amount": "10.0",
            },
        )

        self.client.post(
            "/raclette/add",
            data={
                "date": "2011-08-10",
                "what": "red wine",
                "payer": 2,
                "payed_for": [1],
                "amount": "20",
            },
        )

        self.client.post(
            "/raclette/add",
            data={
                "date": "2011-08-10",
                "what": "delicatessen",
                "payer": 1,
                "payed_for": [1, 2],
                "amount": "10",
            },
        )

        response = self.client.get("/raclette/statistics")
        regex = r"<td class=\"d-md-none\">{}</td>\s+<td>{}</td>\s+<td>{}</td>"
        self.assertRegex(
            response.data.decode("utf-8"),
            regex.format("zorglub", r"\$20\.00", r"\$31\.67"),
        )
        self.assertRegex(
            response.data.decode("utf-8"),
            regex.format("fred", r"\$20\.00", r"\$5\.83"),
        )
        self.assertRegex(
            response.data.decode("utf-8"), regex.format("tata", r"\$0\.00", r"\$2\.50"),
        )
        self.assertRegex(
            response.data.decode("utf-8"), regex.format("pépé", r"\$0\.00", r"\$0\.00"),
        )

        # Check that the order of participants in the sidebar table is the
        # same as in the main table.
        order = ["fred", "pépé", "tata", "zorglub"]
        regex1 = r".*".join(
            r"<td class=\"balance-name\">{}</td>".format(name) for name in order
        )
        regex2 = r".*".join(
            r"<td class=\"d-md-none\">{}</td>".format(name) for name in order
        )
        # Build the regexp ourselves to be able to pass the DOTALL flag
        # (so that ".*" matches newlines)
        self.assertRegex(response.data.decode("utf-8"), re.compile(regex1, re.DOTALL))
        self.assertRegex(response.data.decode("utf-8"), re.compile(regex2, re.DOTALL))

    def test_settle_page(self):
        self.post_project("raclette")
        response = self.client.get("/raclette/settle_bills")
        self.assertEqual(response.status_code, 200)

    def test_settle(self):
        self.post_project("raclette")

        # add members
        self.client.post("/raclette/members/add", data={"name": "zorglub"})
        self.client.post("/raclette/members/add", data={"name": "fred"})
        self.client.post("/raclette/members/add", data={"name": "tata"})
        # Add a member with a balance=0 :
        self.client.post("/raclette/members/add", data={"name": "pépé"})

        # create bills
        self.client.post(
            "/raclette/add",
            data={
                "date": "2011-08-10",
                "what": "fromage à raclette",
                "payer": 1,
                "payed_for": [1, 2, 3],
                "amount": "10.0",
            },
        )

        self.client.post(
            "/raclette/add",
            data={
                "date": "2011-08-10",
                "what": "red wine",
                "payer": 2,
                "payed_for": [1],
                "amount": "20",
            },
        )

        self.client.post(
            "/raclette/add",
            data={
                "date": "2011-08-10",
                "what": "delicatessen",
                "payer": 1,
                "payed_for": [1, 2],
                "amount": "10",
            },
        )
        project = models.Project.query.get("raclette")
        transactions = project.get_transactions_to_settle_bill()
        members = defaultdict(int)
        # We should have the same values between transactions and project balances
        for t in transactions:
            members[t["ower"]] -= t["amount"]
            members[t["receiver"]] += t["amount"]
        balance = models.Project.query.get("raclette").balance
        for m, a in members.items():
            assert abs(a - balance[m.id]) < 0.01
        return

    def test_settle_zero(self):
        self.post_project("raclette")

        # add members
        self.client.post("/raclette/members/add", data={"name": "zorglub"})
        self.client.post("/raclette/members/add", data={"name": "fred"})
        self.client.post("/raclette/members/add", data={"name": "tata"})

        # create bills
        self.client.post(
            "/raclette/add",
            data={
                "date": "2016-12-31",
                "what": "fromage à raclette",
                "payer": 1,
                "payed_for": [1, 2, 3],
                "amount": "10.0",
            },
        )

        self.client.post(
            "/raclette/add",
            data={
                "date": "2016-12-31",
                "what": "red wine",
                "payer": 2,
                "payed_for": [1, 3],
                "amount": "20",
            },
        )

        self.client.post(
            "/raclette/add",
            data={
                "date": "2017-01-01",
                "what": "refund",
                "payer": 3,
                "payed_for": [2],
                "amount": "13.33",
            },
        )
        project = models.Project.query.get("raclette")
        transactions = project.get_transactions_to_settle_bill()

        # There should not be any zero-amount transfer after rounding
        for t in transactions:
            rounded_amount = round(t["amount"], 2)
            self.assertNotEqual(
                0.0,
                rounded_amount,
                msg=f"{t['amount']} is equal to zero after rounding",
            )

    def test_export(self):
        self.post_project("raclette")

        # add members
        self.client.post("/raclette/members/add", data={"name": "zorglub", "weight": 2})
        self.client.post("/raclette/members/add", data={"name": "fred"})
        self.client.post("/raclette/members/add", data={"name": "tata"})
        self.client.post("/raclette/members/add", data={"name": "pépé"})

        # create bills
        self.client.post(
            "/raclette/add",
            data={
                "date": "2016-12-31",
                "what": "fromage à raclette",
                "payer": 1,
                "payed_for": [1, 2, 3, 4],
                "amount": "10.0",
                "original_currency": "USD",
            },
        )

        self.client.post(
            "/raclette/add",
            data={
                "date": "2016-12-31",
                "what": "red wine",
                "payer": 2,
                "payed_for": [1, 3],
                "amount": "200",
                "original_currency": "USD",
            },
        )

        self.client.post(
            "/raclette/add",
            data={
                "date": "2017-01-01",
                "what": "refund",
                "payer": 3,
                "payed_for": [2],
                "amount": "13.33",
                "original_currency": "USD",
            },
        )

        # generate json export of bills
        resp = self.client.get("/raclette/export/bills.json")
        expected = [
            {
                "date": "2017-01-01",
                "what": "refund",
                "amount": 13.33,
                "payer_name": "tata",
                "payer_weight": 1.0,
                "owers": ["fred"],
            },
            {
                "date": "2016-12-31",
                "what": "red wine",
                "amount": 200.0,
                "payer_name": "fred",
                "payer_weight": 1.0,
                "owers": ["zorglub", "tata"],
            },
            {
                "date": "2016-12-31",
                "what": "fromage \xe0 raclette",
                "amount": 10.0,
                "payer_name": "zorglub",
                "payer_weight": 2.0,
                "owers": ["zorglub", "fred", "tata", "p\xe9p\xe9"],
            },
        ]
        self.assertEqual(json.loads(resp.data.decode("utf-8")), expected)

        # generate csv export of bills
        resp = self.client.get("/raclette/export/bills.csv")
        expected = [
            "date,what,amount,payer_name,payer_weight,owers",
            "2017-01-01,refund,13.33,tata,1.0,fred",
            '2016-12-31,red wine,200.0,fred,1.0,"zorglub, tata"',
            '2016-12-31,fromage à raclette,10.0,zorglub,2.0,"zorglub, fred, tata, pépé"',
        ]
        received_lines = resp.data.decode("utf-8").split("\n")

        for i, line in enumerate(expected):
            self.assertEqual(
                set(line.split(",")), set(received_lines[i].strip("\r").split(","))
            )

        # generate json export of transactions
        resp = self.client.get("/raclette/export/transactions.json")
        expected = [
            {"amount": 2.00, "receiver": "fred", "ower": "p\xe9p\xe9"},
            {"amount": 55.34, "receiver": "fred", "ower": "tata"},
            {"amount": 127.33, "receiver": "fred", "ower": "zorglub"},
        ]

        self.assertEqual(json.loads(resp.data.decode("utf-8")), expected)

        # generate csv export of transactions
        resp = self.client.get("/raclette/export/transactions.csv")

        expected = [
            "amount,receiver,ower",
            "2.0,fred,pépé",
            "55.34,fred,tata",
            "127.33,fred,zorglub",
        ]
        received_lines = resp.data.decode("utf-8").split("\n")

        for i, line in enumerate(expected):
            self.assertEqual(
                set(line.split(",")), set(received_lines[i].strip("\r").split(","))
            )

        # wrong export_format should return a 404
        resp = self.client.get("/raclette/export/transactions.wrong")
        self.assertEqual(resp.status_code, 404)

    def test_import_new_project(self):
        # Import JSON in an empty project

        self.post_project("raclette")
        self.login("raclette")

        project = models.Project.query.get("raclette")

        json_to_import = [
            {
                "date": "2017-01-01",
                "what": "refund",
                "amount": 13.33,
                "payer_name": "tata",
                "payer_weight": 1.0,
                "owers": ["fred"],
            },
            {
                "date": "2016-12-31",
                "what": "red wine",
                "amount": 200.0,
                "payer_name": "fred",
                "payer_weight": 1.0,
                "owers": ["zorglub", "tata"],
            },
            {
                "date": "2016-12-31",
                "what": "fromage a raclette",
                "amount": 10.0,
                "payer_name": "zorglub",
                "payer_weight": 2.0,
                "owers": ["zorglub", "fred", "tata", "pepe"],
            },
        ]

        from ihatemoney.web import import_project

        file = io.StringIO()
        json.dump(json_to_import, file)
        file.seek(0)
        import_project(file, project)

        bills = project.get_pretty_bills()

        # Check if all bills has been add
        self.assertEqual(len(bills), len(json_to_import))

        # Check if name of bills are ok
        b = [e["what"] for e in bills]
        b.sort()
        ref = [e["what"] for e in json_to_import]
        ref.sort()

        self.assertEqual(b, ref)

        # Check if other informations in bill are ok
        for i in json_to_import:
            for j in bills:
                if j["what"] == i["what"]:
                    self.assertEqual(j["payer_name"], i["payer_name"])
                    self.assertEqual(j["amount"], i["amount"])
                    self.assertEqual(j["payer_weight"], i["payer_weight"])
                    self.assertEqual(j["date"], i["date"])

                    list_project = [ower for ower in j["owers"]]
                    list_project.sort()
                    list_json = [ower for ower in i["owers"]]
                    list_json.sort()

                    self.assertEqual(list_project, list_json)

    def test_import_partial_project(self):
        # Import a JSON in a project with already existing data

        self.post_project("raclette")
        self.login("raclette")

        project = models.Project.query.get("raclette")

        self.client.post("/raclette/members/add", data={"name": "zorglub", "weight": 2})
        self.client.post("/raclette/members/add", data={"name": "fred"})
        self.client.post("/raclette/members/add", data={"name": "tata"})
        self.client.post(
            "/raclette/add",
            data={
                "date": "2016-12-31",
                "what": "red wine",
                "payer": 2,
                "payed_for": [1, 3],
                "amount": "200",
            },
        )

        json_to_import = [
            {
                "date": "2017-01-01",
                "what": "refund",
                "amount": 13.33,
                "payer_name": "tata",
                "payer_weight": 1.0,
                "owers": ["fred"],
            },
            {  # This expense does not have to be present twice.
                "date": "2016-12-31",
                "what": "red wine",
                "amount": 200.0,
                "payer_name": "fred",
                "payer_weight": 1.0,
                "owers": ["zorglub", "tata"],
            },
            {
                "date": "2016-12-31",
                "what": "fromage a raclette",
                "amount": 10.0,
                "payer_name": "zorglub",
                "payer_weight": 2.0,
                "owers": ["zorglub", "fred", "tata", "pepe"],
            },
        ]

        from ihatemoney.web import import_project

        file = io.StringIO()
        json.dump(json_to_import, file)
        file.seek(0)
        import_project(file, project)

        bills = project.get_pretty_bills()

        # Check if all bills has been add
        self.assertEqual(len(bills), len(json_to_import))

        # Check if name of bills are ok
        b = [e["what"] for e in bills]
        b.sort()
        ref = [e["what"] for e in json_to_import]
        ref.sort()

        self.assertEqual(b, ref)

        # Check if other informations in bill are ok
        for i in json_to_import:
            for j in bills:
                if j["what"] == i["what"]:
                    self.assertEqual(j["payer_name"], i["payer_name"])
                    self.assertEqual(j["amount"], i["amount"])
                    self.assertEqual(j["payer_weight"], i["payer_weight"])
                    self.assertEqual(j["date"], i["date"])

                    list_project = [ower for ower in j["owers"]]
                    list_project.sort()
                    list_json = [ower for ower in i["owers"]]
                    list_json.sort()

                    self.assertEqual(list_project, list_json)

    def test_import_wrong_json(self):
        self.post_project("raclette")
        self.login("raclette")

        project = models.Project.query.get("raclette")

        json_1 = [
            {  # wrong keys
                "checked": False,
                "dimensions": {"width": 5, "height": 10},
                "id": 1,
                "name": "A green door",
                "price": 12.5,
                "tags": ["home", "green"],
            }
        ]

        json_2 = [
            {  # amount missing
                "date": "2017-01-01",
                "what": "refund",
                "payer_name": "tata",
                "payer_weight": 1.0,
                "owers": ["fred"],
            }
        ]

        from ihatemoney.web import import_project

        try:
            file = io.StringIO()
            json.dump(json_1, file)
            file.seek(0)
            import_project(file, project)
        except ValueError:
            self.assertTrue(True)
        except Exception:
            self.fail("unexpected exception raised")
        else:
            self.fail("ExpectedException not raised")

        try:
            file = io.StringIO()
            json.dump(json_2, file)
            file.seek(0)
            import_project(file, project)
        except ValueError:
            self.assertTrue(True)
        except Exception:
            self.fail("unexpected exception raised")
        else:
            self.fail("ExpectedException not raised")


class APITestCase(IhatemoneyTestCase):

    """Tests the API"""

    def api_create(self, name, id=None, password=None, contact=None):
        id = id or name
        password = password or name
        contact = contact or f"{name}@notmyidea.org"

        return self.client.post(
            "/api/projects",
            data={
                "name": name,
                "id": id,
                "password": password,
                "contact_email": contact,
                "default_currency": "USD",
            },
        )

    def api_add_member(self, project, name, weight=1):
        self.client.post(
            f"/api/projects/{project}/members",
            data={"name": name, "weight": weight},
            headers=self.get_auth(project),
        )

    def get_auth(self, username, password=None):
        password = password or username
        base64string = (
            base64.encodebytes(f"{username}:{password}".encode("utf-8"))
            .decode("utf-8")
            .replace("\n", "")
        )
        return {"Authorization": f"Basic {base64string}"}

    def test_cors_requests(self):
        # Create a project and test that CORS headers are present if requested.
        resp = self.api_create("raclette")
        self.assertStatus(201, resp)

        # Try to do an OPTIONS requests and see if the headers are correct.
        resp = self.client.options(
            "/api/projects/raclette", headers=self.get_auth("raclette")
        )
        self.assertEqual(resp.headers["Access-Control-Allow-Origin"], "*")

    def test_basic_auth(self):
        # create a project
        resp = self.api_create("raclette")
        self.assertStatus(201, resp)

        # try to do something on it being unauth should return a 401
        resp = self.client.get("/api/projects/raclette")
        self.assertStatus(401, resp)

        # PUT / POST / DELETE / GET on the different resources
        # should also return a 401
        for verb in ("post",):
            for resource in ("/raclette/members", "/raclette/bills"):
                url = "/api/projects" + resource
                self.assertStatus(401, getattr(self.client, verb)(url), verb + resource)

        for verb in ("get", "delete", "put"):
            for resource in ("/raclette", "/raclette/members/1", "/raclette/bills/1"):
                url = "/api/projects" + resource

                self.assertStatus(401, getattr(self.client, verb)(url), verb + resource)

    def test_project(self):
        # wrong email should return an error
        resp = self.client.post(
            "/api/projects",
            data={
                "name": "raclette",
                "id": "raclette",
                "password": "raclette",
                "contact_email": "not-an-email",
                "default_currency": "USD",
            },
        )

        self.assertTrue(400, resp.status_code)
        self.assertEqual(
            '{"contact_email": ["Invalid email address."]}\n', resp.data.decode("utf-8")
        )

        # create it
        resp = self.api_create("raclette")
        self.assertTrue(201, resp.status_code)

        # create it twice should return a 400
        resp = self.api_create("raclette")

        self.assertTrue(400, resp.status_code)
        self.assertIn("id", json.loads(resp.data.decode("utf-8")))

        # get information about it
        resp = self.client.get(
            "/api/projects/raclette", headers=self.get_auth("raclette")
        )

        self.assertTrue(200, resp.status_code)
        expected = {
            "members": [],
            "name": "raclette",
            "contact_email": "raclette@notmyidea.org",
            "default_currency": "USD",
            "id": "raclette",
            "logging_preference": 1,
        }
        decoded_resp = json.loads(resp.data.decode("utf-8"))
        self.assertDictEqual(decoded_resp, expected)

        # edit should work
        resp = self.client.put(
            "/api/projects/raclette",
            data={
                "contact_email": "yeah@notmyidea.org",
                "default_currency": "USD",
                "password": "raclette",
                "name": "The raclette party",
                "project_history": "y",
            },
            headers=self.get_auth("raclette"),
        )

        self.assertEqual(200, resp.status_code)

        resp = self.client.get(
            "/api/projects/raclette", headers=self.get_auth("raclette")
        )

        self.assertEqual(200, resp.status_code)
        expected = {
            "name": "The raclette party",
            "contact_email": "yeah@notmyidea.org",
            "default_currency": "USD",
            "members": [],
            "id": "raclette",
            "logging_preference": 1,
        }
        decoded_resp = json.loads(resp.data.decode("utf-8"))
        self.assertDictEqual(decoded_resp, expected)

        # password change is possible via API
        resp = self.client.put(
            "/api/projects/raclette",
            data={
                "contact_email": "yeah@notmyidea.org",
                "default_currency": "USD",
                "password": "tartiflette",
                "name": "The raclette party",
            },
            headers=self.get_auth("raclette"),
        )

        self.assertEqual(200, resp.status_code)

        resp = self.client.get(
            "/api/projects/raclette", headers=self.get_auth("raclette", "tartiflette")
        )
        self.assertEqual(200, resp.status_code)

        # delete should work
        resp = self.client.delete(
            "/api/projects/raclette", headers=self.get_auth("raclette", "tartiflette")
        )

        # get should return a 401 on an unknown resource
        resp = self.client.get(
            "/api/projects/raclette", headers=self.get_auth("raclette")
        )
        self.assertEqual(401, resp.status_code)

    def test_token_creation(self):
        """Test that token of project is generated
        """

        # Create project
        resp = self.api_create("raclette")
        self.assertTrue(201, resp.status_code)

        # Get token
        resp = self.client.get(
            "/api/projects/raclette/token", headers=self.get_auth("raclette")
        )

        self.assertEqual(200, resp.status_code)

        decoded_resp = json.loads(resp.data.decode("utf-8"))

        # Access with token
        resp = self.client.get(
            "/api/projects/raclette/token",
            headers={"Authorization": f"Basic {decoded_resp['token']}"},
        )

        self.assertEqual(200, resp.status_code)

    def test_token_login(self):
        resp = self.api_create("raclette")
        # Get token
        resp = self.client.get(
            "/api/projects/raclette/token", headers=self.get_auth("raclette")
        )
        decoded_resp = json.loads(resp.data.decode("utf-8"))
        resp = self.client.get("/authenticate?token={}".format(decoded_resp["token"]))
        # Test that we are redirected.
        self.assertEqual(302, resp.status_code)

    def test_member(self):
        # create a project
        self.api_create("raclette")

        # get the list of members (should be empty)
        req = self.client.get(
            "/api/projects/raclette/members", headers=self.get_auth("raclette")
        )

        self.assertStatus(200, req)
        self.assertEqual("[]\n", req.data.decode("utf-8"))

        # add a member
        req = self.client.post(
            "/api/projects/raclette/members",
            data={"name": "Zorglub"},
            headers=self.get_auth("raclette"),
        )

        # the id of the new member should be returned
        self.assertStatus(201, req)
        self.assertEqual("1\n", req.data.decode("utf-8"))

        # the list of members should contain one member
        req = self.client.get(
            "/api/projects/raclette/members", headers=self.get_auth("raclette")
        )

        self.assertStatus(200, req)
        self.assertEqual(len(json.loads(req.data.decode("utf-8"))), 1)

        # Try to add another member with the same name.
        req = self.client.post(
            "/api/projects/raclette/members",
            data={"name": "Zorglub"},
            headers=self.get_auth("raclette"),
        )
        self.assertStatus(400, req)

        # edit the member
        req = self.client.put(
            "/api/projects/raclette/members/1",
            data={"name": "Fred", "weight": 2},
            headers=self.get_auth("raclette"),
        )

        self.assertStatus(200, req)

        # get should return the new name
        req = self.client.get(
            "/api/projects/raclette/members/1", headers=self.get_auth("raclette")
        )

        self.assertStatus(200, req)
        self.assertEqual("Fred", json.loads(req.data.decode("utf-8"))["name"])
        self.assertEqual(2, json.loads(req.data.decode("utf-8"))["weight"])

        # edit this member with same information
        # (test PUT idemopotence)
        req = self.client.put(
            "/api/projects/raclette/members/1",
            data={"name": "Fred"},
            headers=self.get_auth("raclette"),
        )

        self.assertStatus(200, req)

        # de-activate the user
        req = self.client.put(
            "/api/projects/raclette/members/1",
            data={"name": "Fred", "activated": False},
            headers=self.get_auth("raclette"),
        )
        self.assertStatus(200, req)

        req = self.client.get(
            "/api/projects/raclette/members/1", headers=self.get_auth("raclette")
        )
        self.assertStatus(200, req)
        self.assertEqual(False, json.loads(req.data.decode("utf-8"))["activated"])

        # re-activate the user
        req = self.client.put(
            "/api/projects/raclette/members/1",
            data={"name": "Fred", "activated": True},
            headers=self.get_auth("raclette"),
        )

        req = self.client.get(
            "/api/projects/raclette/members/1", headers=self.get_auth("raclette")
        )
        self.assertStatus(200, req)
        self.assertEqual(True, json.loads(req.data.decode("utf-8"))["activated"])

        # delete a member

        req = self.client.delete(
            "/api/projects/raclette/members/1", headers=self.get_auth("raclette")
        )

        self.assertStatus(200, req)

        # the list of members should be empty
        req = self.client.get(
            "/api/projects/raclette/members", headers=self.get_auth("raclette")
        )

        self.assertStatus(200, req)
        self.assertEqual("[]\n", req.data.decode("utf-8"))

    def test_bills(self):
        # create a project
        self.api_create("raclette")

        # add members
        self.api_add_member("raclette", "zorglub")
        self.api_add_member("raclette", "fred")
        self.api_add_member("raclette", "quentin")

        # get the list of bills (should be empty)
        req = self.client.get(
            "/api/projects/raclette/bills", headers=self.get_auth("raclette")
        )
        self.assertStatus(200, req)

        self.assertEqual("[]\n", req.data.decode("utf-8"))

        # add a bill
        req = self.client.post(
            "/api/projects/raclette/bills",
            data={
                "date": "2011-08-10",
                "what": "fromage",
                "payer": "1",
                "payed_for": ["1", "2"],
                "amount": "25",
                "external_link": "https://raclette.fr",
            },
            headers=self.get_auth("raclette"),
        )

        # should return the id
        self.assertStatus(201, req)
        self.assertEqual(req.data.decode("utf-8"), "1\n")

        # get this bill details
        req = self.client.get(
            "/api/projects/raclette/bills/1", headers=self.get_auth("raclette")
        )

        # compare with the added info
        self.assertStatus(200, req)
        expected = {
            "what": "fromage",
            "payer_id": 1,
            "owers": [
                {"activated": True, "id": 1, "name": "zorglub", "weight": 1},
                {"activated": True, "id": 2, "name": "fred", "weight": 1},
            ],
            "amount": 25.0,
            "date": "2011-08-10",
            "id": 1,
            "converted_amount": 25.0,
            "original_currency": "USD",
            "external_link": "https://raclette.fr",
        }

        got = json.loads(req.data.decode("utf-8"))
        self.assertEqual(
            datetime.date.today(),
            datetime.datetime.strptime(got["creation_date"], "%Y-%m-%d").date(),
        )
        del got["creation_date"]
        self.assertDictEqual(expected, got)

        # the list of bills should length 1
        req = self.client.get(
            "/api/projects/raclette/bills", headers=self.get_auth("raclette")
        )
        self.assertStatus(200, req)
        self.assertEqual(1, len(json.loads(req.data.decode("utf-8"))))

        # edit with errors should return an error
        req = self.client.put(
            "/api/projects/raclette/bills/1",
            data={
                "date": "201111111-08-10",  # not a date
                "what": "fromage",
                "payer": "1",
                "payed_for": ["1", "2"],
                "amount": "25",
                "external_link": "https://raclette.fr",
            },
            headers=self.get_auth("raclette"),
        )

        self.assertStatus(400, req)
        self.assertEqual(
            '{"date": ["This field is required."]}\n', req.data.decode("utf-8")
        )

        # edit a bill
        req = self.client.put(
            "/api/projects/raclette/bills/1",
            data={
                "date": "2011-09-10",
                "what": "beer",
                "payer": "2",
                "payed_for": ["1", "2"],
                "amount": "25",
                "external_link": "https://raclette.fr",
            },
            headers=self.get_auth("raclette"),
        )

        # check its fields
        req = self.client.get(
            "/api/projects/raclette/bills/1", headers=self.get_auth("raclette")
        )
        creation_date = datetime.datetime.strptime(
            json.loads(req.data.decode("utf-8"))["creation_date"], "%Y-%m-%d"
        ).date()

        expected = {
            "what": "beer",
            "payer_id": 2,
            "owers": [
                {"activated": True, "id": 1, "name": "zorglub", "weight": 1},
                {"activated": True, "id": 2, "name": "fred", "weight": 1},
            ],
            "amount": 25.0,
            "date": "2011-09-10",
            "external_link": "https://raclette.fr",
            "converted_amount": 25.0,
            "original_currency": "USD",
            "id": 1,
        }

        got = json.loads(req.data.decode("utf-8"))
        self.assertEqual(
            creation_date,
            datetime.datetime.strptime(got["creation_date"], "%Y-%m-%d").date(),
        )
        del got["creation_date"]
        self.assertDictEqual(expected, got)

        # delete a bill
        req = self.client.delete(
            "/api/projects/raclette/bills/1", headers=self.get_auth("raclette")
        )
        self.assertStatus(200, req)

        # getting it should return a 404
        req = self.client.get(
            "/api/projects/raclette/bills/1", headers=self.get_auth("raclette")
        )
        self.assertStatus(404, req)

    def test_bills_with_calculation(self):
        # create a project
        self.api_create("raclette")

        # add members
        self.api_add_member("raclette", "zorglub")
        self.api_add_member("raclette", "fred")

        # valid amounts
        input_expected = [
            ("((100 + 200.25) * 2 - 100) / 2", 250.25),
            ("3/2", 1.5),
            ("2 + 1 * 5 - 2 / 1", 5),
        ]

        for i, pair in enumerate(input_expected):
            input_amount, expected_amount = pair
            id = i + 1

            req = self.client.post(
                "/api/projects/raclette/bills",
                data={
                    "date": "2011-08-10",
                    "what": "fromage",
                    "payer": "1",
                    "payed_for": ["1", "2"],
                    "amount": input_amount,
                },
                headers=self.get_auth("raclette"),
            )

            # should return the id
            self.assertStatus(201, req)
            self.assertEqual(req.data.decode("utf-8"), "{}\n".format(id))

            # get this bill's details
            req = self.client.get(
                "/api/projects/raclette/bills/{}".format(id),
                headers=self.get_auth("raclette"),
            )

            # compare with the added info
            self.assertStatus(200, req)
            expected = {
                "what": "fromage",
                "payer_id": 1,
                "owers": [
                    {"activated": True, "id": 1, "name": "zorglub", "weight": 1},
                    {"activated": True, "id": 2, "name": "fred", "weight": 1},
                ],
                "amount": expected_amount,
                "date": "2011-08-10",
                "id": id,
                "external_link": "",
                "original_currency": "USD",
                "converted_amount": expected_amount,
            }

            got = json.loads(req.data.decode("utf-8"))
            self.assertEqual(
                datetime.date.today(),
                datetime.datetime.strptime(got["creation_date"], "%Y-%m-%d").date(),
            )
            del got["creation_date"]
            self.assertDictEqual(expected, got)

        # should raise errors
        erroneous_amounts = [
            "lambda ",  # letters
            "(20 + 2",  # invalid expression
            "20/0",  # invalid calc
            "9999**99999999999999999",  # exponents
            "2" * 201,  # greater than 200 chars,
        ]

        for amount in erroneous_amounts:
            req = self.client.post(
                "/api/projects/raclette/bills",
                data={
                    "date": "2011-08-10",
                    "what": "fromage",
                    "payer": "1",
                    "payed_for": ["1", "2"],
                    "amount": amount,
                },
                headers=self.get_auth("raclette"),
            )
            self.assertStatus(400, req)

    def test_statistics(self):
        # create a project
        self.api_create("raclette")

        # add members
        self.api_add_member("raclette", "zorglub")
        self.api_add_member("raclette", "fred")

        # add a bill
        req = self.client.post(
            "/api/projects/raclette/bills",
            data={
                "date": "2011-08-10",
                "what": "fromage",
                "payer": "1",
                "payed_for": ["1", "2"],
                "amount": "25",
            },
            headers=self.get_auth("raclette"),
        )

        # get the list of bills (should be empty)
        req = self.client.get(
            "/api/projects/raclette/statistics", headers=self.get_auth("raclette")
        )
        self.assertStatus(200, req)
        self.assertEqual(
            [
                {
                    "balance": 12.5,
                    "member": {
                        "activated": True,
                        "id": 1,
                        "name": "zorglub",
                        "weight": 1.0,
                    },
                    "paid": 25.0,
                    "spent": 12.5,
                },
                {
                    "balance": -12.5,
                    "member": {
                        "activated": True,
                        "id": 2,
                        "name": "fred",
                        "weight": 1.0,
                    },
                    "paid": 0,
                    "spent": 12.5,
                },
            ],
            json.loads(req.data.decode("utf-8")),
        )

    def test_username_xss(self):
        # create a project
        # self.api_create("raclette")
        self.post_project("raclette")
        self.login("raclette")

        # add members
        self.api_add_member("raclette", "<script>")

        result = self.client.get("/raclette/")
        self.assertNotIn("<script>", result.data.decode("utf-8"))

    def test_weighted_bills(self):
        # create a project
        self.api_create("raclette")

        # add members
        self.api_add_member("raclette", "zorglub")
        self.api_add_member("raclette", "freddy familly", 4)
        self.api_add_member("raclette", "quentin")

        # add a bill
        req = self.client.post(
            "/api/projects/raclette/bills",
            data={
                "date": "2011-08-10",
                "what": "fromage",
                "payer": "1",
                "payed_for": ["1", "2"],
                "amount": "25",
            },
            headers=self.get_auth("raclette"),
        )

        # get this bill details
        req = self.client.get(
            "/api/projects/raclette/bills/1", headers=self.get_auth("raclette")
        )
        creation_date = datetime.datetime.strptime(
            json.loads(req.data.decode("utf-8"))["creation_date"], "%Y-%m-%d"
        ).date()

        # compare with the added info
        self.assertStatus(200, req)
        expected = {
            "what": "fromage",
            "payer_id": 1,
            "owers": [
                {"activated": True, "id": 1, "name": "zorglub", "weight": 1},
                {"activated": True, "id": 2, "name": "freddy familly", "weight": 4},
            ],
            "amount": 25.0,
            "date": "2011-08-10",
            "id": 1,
            "external_link": "",
            "converted_amount": 25.0,
            "original_currency": "USD",
        }
        got = json.loads(req.data.decode("utf-8"))
        self.assertEqual(
            creation_date,
            datetime.datetime.strptime(got["creation_date"], "%Y-%m-%d").date(),
        )
        del got["creation_date"]
        self.assertDictEqual(expected, got)

        # getting it should return a 404
        req = self.client.get(
            "/api/projects/raclette", headers=self.get_auth("raclette")
        )

        expected = {
            "members": [
                {
                    "activated": True,
                    "id": 1,
                    "name": "zorglub",
                    "weight": 1.0,
                    "balance": 20.0,
                },
                {
                    "activated": True,
                    "id": 2,
                    "name": "freddy familly",
                    "weight": 4.0,
                    "balance": -20.0,
                },
                {
                    "activated": True,
                    "id": 3,
                    "name": "quentin",
                    "weight": 1.0,
                    "balance": 0,
                },
            ],
            "contact_email": "raclette@notmyidea.org",
            "id": "raclette",
            "name": "raclette",
            "logging_preference": 1,
            "default_currency": "USD",
        }

        self.assertStatus(200, req)
        decoded_req = json.loads(req.data.decode("utf-8"))
        self.assertDictEqual(decoded_req, expected)

    def test_log_created_from_api_call(self):
        # create a project
        self.api_create("raclette")
        self.login("raclette")

        # add members
        self.api_add_member("raclette", "zorglub")

        resp = self.client.get("/raclette/history", follow_redirects=True)
        self.assertEqual(resp.status_code, 200)
        self.assertIn(
            f"Participant {em_surround('zorglub')} added", resp.data.decode("utf-8")
        )
        self.assertIn(
            f"Project {em_surround('raclette')} added", resp.data.decode("utf-8"),
        )
        self.assertEqual(resp.data.decode("utf-8").count("<td> -- </td>"), 2)
        self.assertNotIn("127.0.0.1", resp.data.decode("utf-8"))


class ServerTestCase(IhatemoneyTestCase):
    def test_homepage(self):
        # See https://github.com/spiral-project/ihatemoney/pull/358
        self.app.config["APPLICATION_ROOT"] = "/"
        req = self.client.get("/")
        self.assertStatus(200, req)

    def test_unprefixed(self):
        self.app.config["APPLICATION_ROOT"] = "/"
        req = self.client.get("/foo/")
        self.assertStatus(303, req)

    def test_prefixed(self):
        self.app.config["APPLICATION_ROOT"] = "/foo"
        req = self.client.get("/foo/")
        self.assertStatus(200, req)


class CommandTestCase(BaseTestCase):
    def test_generate_config(self):
        """ Simply checks that all config file generation
        - raise no exception
        - produce something non-empty
        """
        cmd = GenerateConfig()
        for config_file in cmd.get_options()[0].kwargs["choices"]:
            with patch("sys.stdout", new=io.StringIO()) as stdout:
                cmd.run(config_file)
                print(stdout.getvalue())
                self.assertNotEqual(len(stdout.getvalue().strip()), 0)

    def test_generate_password_hash(self):
        cmd = GeneratePasswordHash()
        with patch("sys.stdout", new=io.StringIO()) as stdout, patch(
            "getpass.getpass", new=lambda prompt: "secret"
        ):  # NOQA
            cmd.run()
            print(stdout.getvalue())
            self.assertEqual(len(stdout.getvalue().strip()), 189)

    def test_demo_project_deletion(self):
        self.create_project("demo")
        self.assertEquals(models.Project.query.get("demo").name, "demo")

        cmd = DeleteProject()
        cmd.run("demo")

        self.assertEqual(len(models.Project.query.all()), 0)


class ModelsTestCase(IhatemoneyTestCase):
    def test_bill_pay_each(self):

        self.post_project("raclette")

        # add members
        self.client.post("/raclette/members/add", data={"name": "zorglub", "weight": 2})
        self.client.post("/raclette/members/add", data={"name": "fred"})
        self.client.post("/raclette/members/add", data={"name": "tata"})
        # Add a member with a balance=0 :
        self.client.post("/raclette/members/add", data={"name": "pépé"})

        # create bills
        self.client.post(
            "/raclette/add",
            data={
                "date": "2011-08-10",
                "what": "fromage à raclette",
                "payer": 1,
                "payed_for": [1, 2, 3],
                "amount": "10.0",
            },
        )

        self.client.post(
            "/raclette/add",
            data={
                "date": "2011-08-10",
                "what": "red wine",
                "payer": 2,
                "payed_for": [1],
                "amount": "20",
            },
        )

        self.client.post(
            "/raclette/add",
            data={
                "date": "2011-08-10",
                "what": "delicatessen",
                "payer": 1,
                "payed_for": [1, 2],
                "amount": "10",
            },
        )

        project = models.Project.query.get_by_name(name="raclette")
        zorglub = models.Person.query.get_by_name(name="zorglub", project=project)
        zorglub_bills = models.Bill.query.options(
            orm.subqueryload(models.Bill.owers)
        ).filter(models.Bill.owers.contains(zorglub))
        for bill in zorglub_bills.all():
            if bill.what == "red wine":
                pay_each_expected = 20 / 2
                self.assertEqual(bill.pay_each(), pay_each_expected)
            if bill.what == "fromage à raclette":
                pay_each_expected = 10 / 4
                self.assertEqual(bill.pay_each(), pay_each_expected)
            if bill.what == "delicatessen":
                pay_each_expected = 10 / 3
                self.assertEqual(bill.pay_each(), pay_each_expected)


class EmailFailureTestCase(IhatemoneyTestCase):
    def test_creation_email_failure_smtp(self):
        self.login("raclette")
        with patch.object(
            self.app.mail, "send", MagicMock(side_effect=smtplib.SMTPException)
        ):
            resp = self.post_project("raclette")
        # Check that an error message is displayed
        self.assertIn(
            "We tried to send you an reminder email, but there was an error",
            resp.data.decode("utf-8"),
        )
        # Check that we were redirected to the home page anyway
        self.assertIn(
            'You probably want to <a href="/raclette/members/add"',
            resp.data.decode("utf-8"),
        )

    def test_creation_email_failure_socket(self):
        self.login("raclette")
        with patch.object(self.app.mail, "send", MagicMock(side_effect=socket.error)):
            resp = self.post_project("raclette")
        # Check that an error message is displayed
        self.assertIn(
            "We tried to send you an reminder email, but there was an error",
            resp.data.decode("utf-8"),
        )
        # Check that we were redirected to the home page anyway
        self.assertIn(
            'You probably want to <a href="/raclette/members/add"',
            resp.data.decode("utf-8"),
        )

    def test_password_reset_email_failure(self):
        self.create_project("raclette")
        for exception in (smtplib.SMTPException, socket.error):
            with patch.object(self.app.mail, "send", MagicMock(side_effect=exception)):
                resp = self.client.post(
                    "/password-reminder", data={"id": "raclette"}, follow_redirects=True
                )
            # Check that an error message is displayed
            self.assertIn(
                "there was an error while sending you an email",
                resp.data.decode("utf-8"),
            )
            # Check that we were not redirected to the success page
            self.assertNotIn(
                "A link to reset your password has been sent to you",
                resp.data.decode("utf-8"),
            )

    def test_invitation_email_failure(self):
        self.login("raclette")
        self.post_project("raclette")
        for exception in (smtplib.SMTPException, socket.error):
            with patch.object(self.app.mail, "send", MagicMock(side_effect=exception)):
                resp = self.client.post(
                    "/raclette/invite",
                    data={"emails": "toto@notmyidea.org"},
                    follow_redirects=True,
                )
            # Check that an error message is displayed
            self.assertIn(
                "there was an error while trying to send the invitation emails",
                resp.data.decode("utf-8"),
            )
            # Check that we are still on the same page (no redirection)
            self.assertIn(
                "Invite people to join this project", resp.data.decode("utf-8"),
            )


def em_surround(string, regex_escape=False):
    if regex_escape:
        return r'<em class="font-italic">%s<\/em>' % string
    else:
        return '<em class="font-italic">%s</em>' % string


class HistoryTestCase(IhatemoneyTestCase):
    def setUp(self):
        super().setUp()
        self.post_project("demo")
        self.login("demo")

    def test_simple_create_logentry_no_ip(self):
        resp = self.client.get("/demo/history")
        self.assertEqual(resp.status_code, 200)
        self.assertIn(
            f"Project {em_surround('demo')} added", resp.data.decode("utf-8"),
        )
        self.assertEqual(resp.data.decode("utf-8").count("<td> -- </td>"), 1)
        self.assertNotIn("127.0.0.1", resp.data.decode("utf-8"))

    def change_privacy_to(self, logging_preference):
        # Change only logging_preferences
        new_data = {
            "name": "demo",
            "contact_email": "demo@notmyidea.org",
            "password": "demo",
            "default_currency": "USD",
        }

        if logging_preference != LoggingMode.DISABLED:
            new_data["project_history"] = "y"
            if logging_preference == LoggingMode.RECORD_IP:
                new_data["ip_recording"] = "y"

        # Disable History
        resp = self.client.post("/demo/edit", data=new_data, follow_redirects=True)
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn("danger", resp.data.decode("utf-8"))

        resp = self.client.get("/demo/edit")
        self.assertEqual(resp.status_code, 200)
        if logging_preference == LoggingMode.DISABLED:
            self.assertIn('<input id="project_history"', resp.data.decode("utf-8"))
        else:
            self.assertIn(
                '<input checked id="project_history"', resp.data.decode("utf-8")
            )

        if logging_preference == LoggingMode.RECORD_IP:
            self.assertIn('<input checked id="ip_recording"', resp.data.decode("utf-8"))
        else:
            self.assertIn('<input id="ip_recording"', resp.data.decode("utf-8"))

    def assert_empty_history_logging_disabled(self):
        resp = self.client.get("/demo/history")
        self.assertIn(
            "This project has history disabled. New actions won't appear below. ",
            resp.data.decode("utf-8"),
        )
        self.assertIn(
            "Nothing to list", resp.data.decode("utf-8"),
        )
        self.assertNotIn(
            "The table below reflects actions recorded prior to disabling project history.",
            resp.data.decode("utf-8"),
        )
        self.assertNotIn(
            "Some entries below contain IP addresses,", resp.data.decode("utf-8"),
        )
        self.assertNotIn("127.0.0.1", resp.data.decode("utf-8"))
        self.assertNotIn("<td> -- </td>", resp.data.decode("utf-8"))
        self.assertNotIn(
            f"Project {em_surround('demo')} added", resp.data.decode("utf-8")
        )

    def test_project_edit(self):
        new_data = {
            "name": "demo2",
            "contact_email": "demo2@notmyidea.org",
            "password": "123456",
            "project_history": "y",
            "default_currency": "USD",
        }

        resp = self.client.post("/demo/edit", data=new_data, follow_redirects=True)
        self.assertEqual(resp.status_code, 200)

        resp = self.client.get("/demo/history")
        self.assertEqual(resp.status_code, 200)
        self.assertIn(f"Project {em_surround('demo')} added", resp.data.decode("utf-8"))
        self.assertIn(
            f"Project contact email changed to {em_surround('demo2@notmyidea.org')}",
            resp.data.decode("utf-8"),
        )
        self.assertIn(
            "Project private code changed", resp.data.decode("utf-8"),
        )
        self.assertIn(
            f"Project renamed to {em_surround('demo2')}", resp.data.decode("utf-8"),
        )
        self.assertLess(
            resp.data.decode("utf-8").index("Project renamed "),
            resp.data.decode("utf-8").index("Project contact email changed to "),
        )
        self.assertLess(
            resp.data.decode("utf-8").index("Project renamed "),
            resp.data.decode("utf-8").index("Project private code changed"),
        )
        self.assertEqual(resp.data.decode("utf-8").count("<td> -- </td>"), 4)
        self.assertNotIn("127.0.0.1", resp.data.decode("utf-8"))

    def test_project_privacy_edit(self):
        resp = self.client.get("/demo/edit")
        self.assertEqual(resp.status_code, 200)
        self.assertIn(
            '<input checked id="project_history" name="project_history" type="checkbox" value="y">',
            resp.data.decode("utf-8"),
        )

        self.change_privacy_to(LoggingMode.DISABLED)

        resp = self.client.get("/demo/history")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Disabled Project History\n", resp.data.decode("utf-8"))
        self.assertEqual(resp.data.decode("utf-8").count("<td> -- </td>"), 2)
        self.assertNotIn("127.0.0.1", resp.data.decode("utf-8"))

        self.change_privacy_to(LoggingMode.RECORD_IP)

        resp = self.client.get("/demo/history")
        self.assertEqual(resp.status_code, 200)
        self.assertIn(
            "Enabled Project History & IP Address Recording", resp.data.decode("utf-8")
        )
        self.assertEqual(resp.data.decode("utf-8").count("<td> -- </td>"), 2)
        self.assertEqual(resp.data.decode("utf-8").count("127.0.0.1"), 1)

        self.change_privacy_to(LoggingMode.ENABLED)

        resp = self.client.get("/demo/history")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Disabled IP Address Recording\n", resp.data.decode("utf-8"))
        self.assertEqual(resp.data.decode("utf-8").count("<td> -- </td>"), 2)
        self.assertEqual(resp.data.decode("utf-8").count("127.0.0.1"), 2)

    def test_project_privacy_edit2(self):
        self.change_privacy_to(LoggingMode.RECORD_IP)

        resp = self.client.get("/demo/history")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Enabled IP Address Recording\n", resp.data.decode("utf-8"))
        self.assertEqual(resp.data.decode("utf-8").count("<td> -- </td>"), 1)
        self.assertEqual(resp.data.decode("utf-8").count("127.0.0.1"), 1)

        self.change_privacy_to(LoggingMode.DISABLED)

        resp = self.client.get("/demo/history")
        self.assertEqual(resp.status_code, 200)
        self.assertIn(
            "Disabled Project History & IP Address Recording", resp.data.decode("utf-8")
        )
        self.assertEqual(resp.data.decode("utf-8").count("<td> -- </td>"), 1)
        self.assertEqual(resp.data.decode("utf-8").count("127.0.0.1"), 2)

        self.change_privacy_to(LoggingMode.ENABLED)

        resp = self.client.get("/demo/history")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Enabled Project History\n", resp.data.decode("utf-8"))
        self.assertEqual(resp.data.decode("utf-8").count("<td> -- </td>"), 2)
        self.assertEqual(resp.data.decode("utf-8").count("127.0.0.1"), 2)

    def do_misc_database_operations(self, logging_mode):
        new_data = {
            "name": "demo2",
            "contact_email": "demo2@notmyidea.org",
            "password": "123456",
            "default_currency": "USD",
        }

        # Keep privacy settings where they were
        if logging_mode != LoggingMode.DISABLED:
            new_data["project_history"] = "y"
            if logging_mode == LoggingMode.RECORD_IP:
                new_data["ip_recording"] = "y"

        resp = self.client.post("/demo/edit", data=new_data, follow_redirects=True)
        self.assertEqual(resp.status_code, 200)

        # adds a member to this project
        resp = self.client.post(
            "/demo/members/add", data={"name": "zorglub"}, follow_redirects=True
        )
        self.assertEqual(resp.status_code, 200)

        user_id = models.Person.query.one().id

        # create a bill
        resp = self.client.post(
            "/demo/add",
            data={
                "date": "2011-08-10",
                "what": "fromage à raclette",
                "payer": user_id,
                "payed_for": [user_id],
                "amount": "25",
            },
            follow_redirects=True,
        )
        self.assertEqual(resp.status_code, 200)

        bill_id = models.Bill.query.one().id

        # edit the bill
        resp = self.client.post(
            f"/demo/edit/{bill_id}",
            data={
                "date": "2011-08-10",
                "what": "fromage à raclette",
                "payer": user_id,
                "payed_for": [user_id],
                "amount": "10",
            },
            follow_redirects=True,
        )
        self.assertEqual(resp.status_code, 200)
        # delete the bill
        resp = self.client.get(f"/demo/delete/{bill_id}", follow_redirects=True)
        self.assertEqual(resp.status_code, 200)

        # delete user using POST method
        resp = self.client.post(
            f"/demo/members/{user_id}/delete", follow_redirects=True
        )
        self.assertEqual(resp.status_code, 200)

    def test_disable_clear_no_new_records(self):
        # Disable logging
        self.change_privacy_to(LoggingMode.DISABLED)

        resp = self.client.get("/demo/history")
        self.assertEqual(resp.status_code, 200)
        self.assertIn(
            "This project has history disabled. New actions won't appear below. ",
            resp.data.decode("utf-8"),
        )
        self.assertIn(
            "The table below reflects actions recorded prior to disabling project history.",
            resp.data.decode("utf-8"),
        )
        self.assertNotIn(
            "Nothing to list", resp.data.decode("utf-8"),
        )
        self.assertNotIn(
            "Some entries below contain IP addresses,", resp.data.decode("utf-8"),
        )

        # Clear Existing Entries
        resp = self.client.post("/demo/erase_history", follow_redirects=True)
        self.assertEqual(resp.status_code, 200)
        self.assert_empty_history_logging_disabled()

        # Do lots of database operations & check that there's still no history
        self.do_misc_database_operations(LoggingMode.DISABLED)

        self.assert_empty_history_logging_disabled()

    def test_clear_ip_records(self):
        # Enable IP Recording
        self.change_privacy_to(LoggingMode.RECORD_IP)

        # Do lots of database operations to generate IP address entries
        self.do_misc_database_operations(LoggingMode.RECORD_IP)

        # Disable IP Recording
        self.change_privacy_to(LoggingMode.ENABLED)

        resp = self.client.get("/demo/history")
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn(
            "This project has history disabled. New actions won't appear below. ",
            resp.data.decode("utf-8"),
        )
        self.assertNotIn(
            "The table below reflects actions recorded prior to disabling project history.",
            resp.data.decode("utf-8"),
        )
        self.assertNotIn(
            "Nothing to list", resp.data.decode("utf-8"),
        )
        self.assertIn(
            "Some entries below contain IP addresses,", resp.data.decode("utf-8"),
        )
        self.assertEqual(resp.data.decode("utf-8").count("127.0.0.1"), 10)
        self.assertEqual(resp.data.decode("utf-8").count("<td> -- </td>"), 1)

        # Generate more operations to confirm additional IP info isn't recorded
        self.do_misc_database_operations(LoggingMode.ENABLED)

        resp = self.client.get("/demo/history")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data.decode("utf-8").count("127.0.0.1"), 10)
        self.assertEqual(resp.data.decode("utf-8").count("<td> -- </td>"), 6)

        # Clear IP Data
        resp = self.client.post("/demo/strip_ip_addresses", follow_redirects=True)
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn(
            "This project has history disabled. New actions won't appear below. ",
            resp.data.decode("utf-8"),
        )
        self.assertNotIn(
            "The table below reflects actions recorded prior to disabling project history.",
            resp.data.decode("utf-8"),
        )
        self.assertNotIn(
            "Nothing to list", resp.data.decode("utf-8"),
        )
        self.assertNotIn(
            "Some entries below contain IP addresses,", resp.data.decode("utf-8"),
        )
        self.assertEqual(resp.data.decode("utf-8").count("127.0.0.1"), 0)
        self.assertEqual(resp.data.decode("utf-8").count("<td> -- </td>"), 16)

    def test_logs_for_common_actions(self):
        # adds a member to this project
        resp = self.client.post(
            "/demo/members/add", data={"name": "zorglub"}, follow_redirects=True
        )
        self.assertEqual(resp.status_code, 200)

        resp = self.client.get("/demo/history")
        self.assertEqual(resp.status_code, 200)
        self.assertIn(
            f"Participant {em_surround('zorglub')} added", resp.data.decode("utf-8")
        )

        # create a bill
        resp = self.client.post(
            "/demo/add",
            data={
                "date": "2011-08-10",
                "what": "fromage à raclette",
                "payer": 1,
                "payed_for": [1],
                "amount": "25",
            },
            follow_redirects=True,
        )
        self.assertEqual(resp.status_code, 200)

        resp = self.client.get("/demo/history")
        self.assertEqual(resp.status_code, 200)
        self.assertIn(
            f"Bill {em_surround('fromage à raclette')} added",
            resp.data.decode("utf-8"),
        )

        # edit the bill
        resp = self.client.post(
            "/demo/edit/1",
            data={
                "date": "2011-08-10",
                "what": "new thing",
                "payer": 1,
                "payed_for": [1],
                "amount": "10",
            },
            follow_redirects=True,
        )
        self.assertEqual(resp.status_code, 200)

        resp = self.client.get("/demo/history")
        self.assertEqual(resp.status_code, 200)
        self.assertIn(
            f"Bill {em_surround('fromage à raclette')} added",
            resp.data.decode("utf-8"),
        )
        self.assertRegex(
            resp.data.decode("utf-8"),
            r"Bill %s:\s* Amount changed\s* from %s\s* to %s"
            % (
                em_surround("fromage à raclette", regex_escape=True),
                em_surround("25.0", regex_escape=True),
                em_surround("10.0", regex_escape=True),
            ),
        )
        self.assertIn(
            "Bill %s renamed to %s"
            % (em_surround("fromage à raclette"), em_surround("new thing"),),
            resp.data.decode("utf-8"),
        )
        self.assertLess(
            resp.data.decode("utf-8").index(
                f"Bill {em_surround('fromage à raclette')} renamed to"
            ),
            resp.data.decode("utf-8").index("Amount changed"),
        )

        # delete the bill
        resp = self.client.get("/demo/delete/1", follow_redirects=True)
        self.assertEqual(resp.status_code, 200)

        resp = self.client.get("/demo/history")
        self.assertEqual(resp.status_code, 200)
        self.assertIn(
            f"Bill {em_surround('new thing')} removed", resp.data.decode("utf-8"),
        )

        # edit user
        resp = self.client.post(
            "/demo/members/1/edit",
            data={"weight": 2, "name": "new name"},
            follow_redirects=True,
        )
        self.assertEqual(resp.status_code, 200)

        resp = self.client.get("/demo/history")
        self.assertEqual(resp.status_code, 200)
        self.assertRegex(
            resp.data.decode("utf-8"),
            r"Participant %s:\s* Weight changed\s* from %s\s* to %s"
            % (
                em_surround("zorglub", regex_escape=True),
                em_surround("1.0", regex_escape=True),
                em_surround("2.0", regex_escape=True),
            ),
        )
        self.assertIn(
            "Participant %s renamed to %s"
            % (em_surround("zorglub"), em_surround("new name"),),
            resp.data.decode("utf-8"),
        )
        self.assertLess(
            resp.data.decode("utf-8").index(
                f"Participant {em_surround('zorglub')} renamed"
            ),
            resp.data.decode("utf-8").index("Weight changed"),
        )

        # delete user using POST method
        resp = self.client.post("/demo/members/1/delete", follow_redirects=True)
        self.assertEqual(resp.status_code, 200)

        resp = self.client.get("/demo/history")
        self.assertEqual(resp.status_code, 200)
        self.assertIn(
            f"Participant {em_surround('new name')} removed", resp.data.decode("utf-8")
        )

    def test_double_bill_double_person_edit_second(self):

        # add two members
        self.client.post("/demo/members/add", data={"name": "User 1"})
        self.client.post("/demo/members/add", data={"name": "User 2"})

        # add two bills
        self.client.post(
            "/demo/add",
            data={
                "date": "2020-04-13",
                "what": "Bill 1",
                "payer": 1,
                "payed_for": [1, 2],
                "amount": "25",
            },
        )
        self.client.post(
            "/demo/add",
            data={
                "date": "2020-04-13",
                "what": "Bill 2",
                "payer": 1,
                "payed_for": [1, 2],
                "amount": "20",
            },
        )

        # Should be 5 history entries at this point
        resp = self.client.get("/demo/history")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data.decode("utf-8").count("<td> -- </td>"), 5)
        self.assertNotIn("127.0.0.1", resp.data.decode("utf-8"))

        # Edit ONLY the amount on the first bill
        self.client.post(
            "/demo/edit/1",
            data={
                "date": "2020-04-13",
                "what": "Bill 1",
                "payer": 1,
                "payed_for": [1, 2],
                "amount": "88",
            },
        )

        resp = self.client.get("/demo/history")
        self.assertEqual(resp.status_code, 200)
        self.assertRegex(
            resp.data.decode("utf-8"),
            r"Bill {}:\s* Amount changed\s* from {}\s* to {}".format(
                em_surround("Bill 1", regex_escape=True),
                em_surround("25.0", regex_escape=True),
                em_surround("88.0", regex_escape=True),
            ),
        )

        self.assertNotRegex(
            resp.data.decode("utf-8"),
            r"Removed\s* {}\s* and\s* {}\s* from\s* owers list".format(
                em_surround("User 1", regex_escape=True),
                em_surround("User 2", regex_escape=True),
            ),
            resp.data.decode("utf-8"),
        )

        # Should be 6 history entries at this point
        self.assertEqual(resp.data.decode("utf-8").count("<td> -- </td>"), 6)
        self.assertNotIn("127.0.0.1", resp.data.decode("utf-8"))

    def test_bill_add_remove_add(self):
        # add two members
        self.client.post("/demo/members/add", data={"name": "User 1"})
        self.client.post("/demo/members/add", data={"name": "User 2"})

        # add 1 bill
        self.client.post(
            "/demo/add",
            data={
                "date": "2020-04-13",
                "what": "Bill 1",
                "payer": 1,
                "payed_for": [1, 2],
                "amount": "25",
            },
        )

        # delete the bill
        self.client.get("/demo/delete/1", follow_redirects=True)

        resp = self.client.get("/demo/history")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data.decode("utf-8").count("<td> -- </td>"), 5)
        self.assertNotIn("127.0.0.1", resp.data.decode("utf-8"))
        self.assertIn(f"Bill {em_surround('Bill 1')} added", resp.data.decode("utf-8"))
        self.assertIn(
            f"Bill {em_surround('Bill 1')} removed", resp.data.decode("utf-8"),
        )

        # Add a new bill
        self.client.post(
            "/demo/add",
            data={
                "date": "2020-04-13",
                "what": "Bill 2",
                "payer": 1,
                "payed_for": [1, 2],
                "amount": "20",
            },
        )

        resp = self.client.get("/demo/history")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data.decode("utf-8").count("<td> -- </td>"), 6)
        self.assertNotIn("127.0.0.1", resp.data.decode("utf-8"))
        self.assertIn(f"Bill {em_surround('Bill 1')} added", resp.data.decode("utf-8"))
        self.assertEqual(
            resp.data.decode("utf-8").count(f"Bill {em_surround('Bill 1')} added"), 1,
        )
        self.assertIn(f"Bill {em_surround('Bill 2')} added", resp.data.decode("utf-8"))
        self.assertIn(
            f"Bill {em_surround('Bill 1')} removed", resp.data.decode("utf-8"),
        )

    def test_double_bill_double_person_edit_second_no_web(self):
        u1 = models.Person(project_id="demo", name="User 1")
        u2 = models.Person(project_id="demo", name="User 1")

        models.db.session.add(u1)
        models.db.session.add(u2)
        models.db.session.commit()

        b1 = models.Bill(what="Bill 1", payer_id=u1.id, owers=[u2], amount=10,)
        b2 = models.Bill(what="Bill 2", payer_id=u2.id, owers=[u2], amount=11,)

        # This db commit exposes the "spurious owers edit" bug
        models.db.session.add(b1)
        models.db.session.commit()

        models.db.session.add(b2)
        models.db.session.commit()

        history_list = history.get_history(models.Project.query.get("demo"))
        self.assertEqual(len(history_list), 5)

        # Change just the amount
        b1.amount = 5
        models.db.session.commit()

        history_list = history.get_history(models.Project.query.get("demo"))
        for entry in history_list:
            if "prop_changed" in entry:
                self.assertNotIn("owers", entry["prop_changed"])
        self.assertEqual(len(history_list), 6)


class TestCurrencyConverter(unittest.TestCase):
    converter = CurrencyConverter()
    mock_data = {"USD": 1, "EUR": 0.8115}
    converter.get_rates = MagicMock(return_value=mock_data)

    def test_only_one_instance(self):
        one = id(CurrencyConverter())
        two = id(CurrencyConverter())
        self.assertEqual(one, two)

    def test_get_currencies(self):
        self.assertCountEqual(self.converter.get_currencies(), ["USD", "EUR"])

    def test_exchange_currency(self):
        result = self.converter.exchange_currency(100, "USD", "EUR")
        self.assertEqual(result, 81.15)


if __name__ == "__main__":
    unittest.main()
