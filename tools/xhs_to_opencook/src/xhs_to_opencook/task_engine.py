"""Framework-driven single-task execution; journals contain facts, never framework objects."""

from copy import deepcopy
from threading import RLock, get_ident
from uuid import uuid4

import py_trees
from transitions import Machine


class ObserveAnswer(py_trees.behaviour.Behaviour):
    def __init__(self, executor):
        super().__init__('Observe current request')
        self.executor = executor

    def update(self):
        executor = self.executor
        snapshot = executor.browser.observe(deepcopy(executor.record['request']))
        if snapshot.get('matched') and snapshot.get('finished') and snapshot.get('answer'):
            executor.record['answer'] = snapshot['answer']
            return py_trees.common.Status.SUCCESS
        return py_trees.common.Status.RUNNING

    def terminate(self, new_status):
        if new_status == py_trees.common.Status.INVALID:
            self.executor.browser.cancel_observation()


class TaskExecutor:
    """One owner-thread tick advances one task step without sleeping.

    browser supplies submit/reconcile/observe/cancel_observation; journal supplies
    load/save. Real adapters must make observe a bounded snapshot operation.
    """

    def __init__(self, identity, browser, journal, *, clock):
        self.identity, self.browser, self.journal, self.clock = identity, browser, journal, clock
        self.owner = get_ident()
        self.lock = RLock()
        self.record = journal.load(identity) or {
            'task_id': identity, 'phase': 'preparing', 'paused': False,
            'request': {'request_id': uuid4().hex, 'status': 'prepared'},
        }
        self.needs_reconciliation = self.record['phase'] == 'waiting' or (
            self.record['phase'] == 'submitting'
            and self.record['request']['status'] != 'prepared'
        )
        self.machine = Machine(
            model=self, states=['preparing', 'submitting', 'waiting', 'validating'],
            initial=self.record['phase'], auto_transitions=False, queued=True,
            transitions=[
                ['prepared', 'preparing', 'submitting'],
                ['submitted', 'submitting', 'waiting'],
                ['answered', 'waiting', 'validating'],
            ],
        )
        self.tree = py_trees.trees.BehaviourTree(ObserveAnswer(self))

    def snapshot(self):
        return deepcopy(self.record)

    def _save(self):
        self.record['phase'] = self.state
        self.record['updated_at'] = self.clock()
        self.journal.save(self.identity, self.record)

    def tick(self):
        if get_ident() != self.owner:
            raise RuntimeError('Task ticks must run on the browser owner thread')
        with self.lock:
            if self.record['paused']:
                return self.snapshot()
            if self.needs_reconciliation:
                snapshot = self.browser.reconcile(deepcopy(self.record['request']))
                if not snapshot.get('submitted'):
                    return self.snapshot()
                self.record['request']['status'] = 'submitted'
                if self.state == 'submitting':
                    self.submitted()
                self.needs_reconciliation = False
            elif self.state == 'preparing':
                self.prepared()
            elif self.state == 'submitting':
                self.record['request']['status'] = 'sending'
                self._save()
                snapshot = self.browser.submit(deepcopy(self.record['request']))
                if snapshot.get('submitted'):
                    self.record['request']['status'] = 'submitted'
                    self.submitted()
                else:
                    self.needs_reconciliation = True
            elif self.state == 'waiting':
                self.tree.tick()
                if self.tree.root.status == py_trees.common.Status.SUCCESS:
                    self.answered()
            self._save()
            return self.snapshot()

    def pause(self):
        with self.lock:
            self.tree.interrupt()
            self.tree.root.stop(py_trees.common.Status.INVALID)
            self.record['paused'] = True
            self._save()

    def resume(self):
        with self.lock:
            self.record['paused'] = False
            self.needs_reconciliation = self.state in {'submitting', 'waiting'}
            self._save()
