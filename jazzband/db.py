from collections import deque
from contextlib import contextmanager

from flask_redis import FlaskRedis
from flask_sqlalchemy import Model, SQLAlchemy
from walrus import Walrus

from .exceptions import Rollback


class JazzbandModel(Model):
    @classmethod
    def update_or_create(cls, defaults=None, commit=True, **kwargs):
        if defaults is None:
            defaults = {}
        instance = cls.query.filter_by(**kwargs).first()
        if instance:
            for arg, value in defaults.items():
                setattr(instance, arg, value)
            if commit:
                postgres.session.commit()
            return instance, False
        else:
            params = kwargs.copy()
            params.update(defaults)
            instance = cls(**params)
            postgres.session.add(instance)
            if commit:
                postgres.session.commit()
            return instance, True

    def save(self, commit=True):
        postgres.session.add(self)
        if commit:
            postgres.session.commit()
        return self

    def delete(self, commit=True):
        postgres.session.delete(self)
        if commit:
            postgres.session.commit()
        return self


class JazzbandSQLAlchemy(SQLAlchemy):
    def init_app(self, app):
        super().init_app(app)
        app.config.setdefault("SQLALCHEMY_NESTED_TRANSACTION", False)
        app.config.setdefault("SQLALCHEMY_ISOLATE_TRANSACTION", True)
        # dispose of engine to fix issue with forks
        # https://virtualandy.wordpress.com/2019/09/04/a-fix-for-operationalerror-psycopg2-operationalerror-ssl-error-decryption-failed-or-bad-record-mac/
        with app.app_context():
            self.engine.dispose()

    @contextmanager
    def transaction(self, isolate=None, nested=None, **kwargs):
        """Safely commits if no errors, will rollback otherwise.

        This is preferably used with PEP 343 `with` statement, for example:

            with db.transaction():
                db.session.execute(...)

        If `execute` succeeds without any exception, `commit` will be emitted;
        or else if any exception (but ``Rollback`` in certain cases, see below)
        is raised within the `with` block, or even if the implicit `commit`
        fails, a `rollback` is guaranteed at the end of the `with` block.

        In some cases, you may want to manually rollback the transaction from
        inside. Generally you can simply raise any exception to abort the
        transaction; alternatively there is a special exception ``Rollback``,
        with which you can choose to let ``db.transaction`` handle the
        exception. Please see ``Rollback`` for more information.

        By default when `autocommit=False`, there is always an open transaction
        (not necessarily DB-level) associated with any session object. In such
        case, it is a common usage that, DB access can be performed anytime
        whenever there is a session, and do commit or rollback manually
        whenever they are needed. This is convenient and widely adopted, but it
        creates a mess over transaction boundary - what **exactly** is included
        when commit happens? So by default, when entering a `db.transaction`
        block, a `rollback` is executed when the situation is not clear, in
        order to isolate the transaction boundary to precisely where it is
        defined.

        And of course this behavior can be altered, globally by setting config
        `SQLALCHEMY_ISOLATE_TRANSACTION` to `False`, or explicitly by setting
        `isolate=False` on a `db.transaction` call. Latter overrides former.

        Though `autocommit=True` is no recommended by SQLAlchemy, it is anyway
        supported here. Entering `db.transaction` ensures a `begin`, the rest
        stays all the same as described above.

        Transactions can be nested, without setting the parameter `nested`,
        which is used to select between the two different nested transaction
        implementations - subtransaction (default) or savepoint. With
        subtransactions, it is programed to guarantee that only all
        subtransactions are committed can the DB transaction be committed; any
        rollback in subtransactions - even if the exception is captured - will
        lead the DB transaction to be rolled back (not immediately), commit
        attempts on parent transactions shall simply fail. Differently with
        savepoint, one can rollback to a savepoint and carry on in the same
        transaction, and possibly commit it. Nested transactions are suitable
        for cases when a reused function needs to guarantee its logic is at
        least atomic when called separately, while it can also be embed into
        another transaction as a whole.

        The default nested transaction implementation is not **nested** - a
        keyword reserved by SQLAlchemy to indicate using savepoint, reused here
        to follow the same custom. It can be globally set to use savepoint by
        setting config `SQLALCHEMY_NESTED_TRANSACTION` to `True`;
        alternatively it can be overriden by setting `nested` parameter on a
        `db.transaction` call.

        :param isolate:
            `True`: guarantee transaction boundary;
            `False`: do not rollback at the beginning;
            `None`(default): follow config `SQLALCHEMY_ISOLATE_TRANSACTION`
        :param nested:
            `True`: use savepoint for nested transaction;
            `False`: use subtransaction for nested transaction;
            `None`(default): follow config `SQLALCHEMY_NESTED_TRANSACTION`
        :param kwargs:
            additional key-value pairs to be set in the transaction-local
        :return: a PEP 343 context object to be used by `with`
        """
        session = self.session()
        try:
            stack = session._tx_stack
        except AttributeError:
            stack = session._tx_stack = deque()
        is_root = len(stack) == 0

        if is_root:
            nested = False
            item = {}
        else:
            item = stack[-1].copy()

        if nested is None:
            nested = self.get_app().config["SQLALCHEMY_NESTED_TRANSACTION"]
        if isolate is None:
            isolate = self.get_app().config["SQLALCHEMY_ISOLATE_TRANSACTION"]

        item.update(kwargs)
        stack.append(item)
        try:
            if is_root and not session.autocommit:
                if isolate:
                    session.rollback()
            else:
                session.begin(subtransactions=True, nested=nested)
            try:
                yield
                session.commit()
            except Rollback as e:
                session.rollback()
                if e.propagate:
                    raise
                if e.propagate is None and not nested:
                    raise
            except Exception:
                session.rollback()
                raise
        finally:
            stack.pop()

    @property
    def tx_local(self):
        """A shared dict object associated with current (nested) transaction"""
        stack = getattr(self.session(), "_tx_stack", None)
        if stack:
            return stack[-1]

    @property
    def root_tx_local(self):
        """A shared dict object associated with current DB transaction"""
        stack = getattr(self.session(), "_tx_stack", None)
        if stack:
            return stack[0]


postgres = JazzbandSQLAlchemy(model_class=JazzbandModel)

redis = FlaskRedis.from_custom_provider(Walrus)
