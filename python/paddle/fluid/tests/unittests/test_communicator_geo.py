#   Copyright (c) 2019 PaddlePaddle Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import print_function

import os
import sys
import time
import threading
import subprocess
import unittest
import numpy

import paddle
import paddle.fluid as fluid

import paddle.fluid.incubate.fleet.base.role_maker as role_maker
from paddle.fluid.incubate.fleet.parameter_server.distribute_transpiler import fleet
from paddle.fluid.incubate.fleet.parameter_server.distribute_transpiler.distributed_strategy import StrategyFactory


class TestCommunicatorGeoEnd2End(unittest.TestCase):
    def net(self):
        x = fluid.layers.data(name='x', shape=[13], dtype='float32')
        y_predict = fluid.layers.fc(input=x, size=1, act=None)
        y = fluid.layers.data(name='y', shape=[1], dtype='float32')

        cost = fluid.layers.square_error_cost(input=y_predict, label=y)
        avg_cost = fluid.layers.mean(cost)
        return avg_cost, x, y

    def fake_reader(self):
        def reader():
            for i in range(10000):
                x = numpy.random.random((1, 13)).astype('float32')
                y = numpy.random.randint(0, 2, (1, 1)).astype('int64')
                yield x, y

        return reader

    def run_pserver(self, role, strategy):
        fleet.init(role)
        avg_cost, x, y = self.net()
        optimizer = fluid.optimizer.SGD(0.01)
        optimizer = fleet.distributed_optimizer(optimizer, strategy)
        optimizer.minimize(avg_cost)

        fleet.init_server()
        fleet.run_server()

    def run_trainer(self, role, strategy):
        place = fluid.core.CPUPlace()
        exe = fluid.Executor(place)

        fleet.init(role)
        avg_cost, x, y = self.net()
        optimizer = fluid.optimizer.SGD(0.01)
        optimizer = fleet.distributed_optimizer(optimizer, strategy)
        optimizer.minimize(avg_cost)

        fleet.init_worker()
        exe.run(fleet.startup_program)

        train_reader = paddle.batch(self.fake_reader(), batch_size=24)
        feeder = fluid.DataFeeder(place=place, feed_list=[x, y])

        for batch_id, data in enumerate(train_reader()):
            exe.run(fleet.main_program, feed=feeder.feed(data), fetch_list=[])

        fleet.stop_worker()

    def run_ut(self):
        training_role = os.getenv("TRAINING_ROLE", "TRAINER")

        role = role_maker.UserDefinedRoleMaker(
            current_id=0,
            role=role_maker.Role.WORKER
            if training_role == "TRAINER" else role_maker.Role.SERVER,
            worker_num=1,
            server_endpoints=["127.0.0.1:18099"])

        strategy = StrategyFactory.create_geo_strategy(10)

        if training_role == "TRAINER":
            self.run_trainer(role, strategy)
        else:
            self.run_pserver(role, strategy)

    def test_communicator(self):
        run_server_cmd = """
from __future__ import print_function

import sys
import os

import time
import threading
import subprocess
import unittest
import numpy

import paddle
import paddle.fluid as fluid

from paddle.fluid.communicator import Communicator
import paddle.fluid.incubate.fleet.base.role_maker as role_maker
from paddle.fluid.incubate.fleet.parameter_server.mode import DistributedMode
from paddle.fluid.incubate.fleet.parameter_server.distribute_transpiler import fleet
from paddle.fluid.incubate.fleet.parameter_server.distribute_transpiler.distributed_strategy import StrategyFactory

from test_communicator_geo import TestCommunicatorGeoEnd2End


class RunServer(TestCommunicatorGeoEnd2End):
    def runTest(self):
        pass

os.environ["TRAINING_ROLE"] = "PSERVER"
os.environ["http_proxy"] = ""
os.environ["https_proxy"] = ""

half_run_server = RunServer()
half_run_server.run_ut()
"""

        server_file = "run_server_for_communicator_geo.py"
        with open(server_file, "w") as wb:
            wb.write(run_server_cmd)
        os.environ["TRAINING_ROLE"] = "PSERVER"
        os.environ["http_proxy"] = ""
        os.environ["https_proxy"] = ""

        _python = sys.executable

        ps_cmd = "{} {}".format(_python, server_file)
        ps_proc = subprocess.Popen(
            ps_cmd.strip().split(" "),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE)

        time.sleep(5)

        os.environ["TRAINING_ROLE"] = "TRAINER"
        os.environ["http_proxy"] = ""
        os.environ["https_proxy"] = ""

        self.run_ut()
        ps_proc.kill()

        if os.path.exists(server_file):
            os.remove(server_file)


if __name__ == '__main__':
    unittest.main()
