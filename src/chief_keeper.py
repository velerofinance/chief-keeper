# This file is part of the Maker Keeper Framework.
#
# Copyright (C) 2019 KentonPrescott
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import argparse
import logging
import sys
import time
from datetime import datetime, timezone
import types
import os
from typing import List

from tinydb import TinyDB, Query
from web3 import Web3, HTTPProvider



from pymaker import Address, Contract
from pymaker.util import is_contract_at
from pymaker.gas import DefaultGasPrice, FixedGasPrice
from pymaker.auctions import Flipper, Flapper, Flopper
from pymaker.keys import register_keys
from pymaker.lifecycle import Lifecycle
from pymaker.numeric import Wad, Rad, Ray
from pymaker.token import ERC20Token
from pymaker.deployment import DssDeployment
from pymaker.dss import Ilk, Urn

class DSSSpell(Contract):
    """A client for the `DSPause` contract, which schedules function calls after a predefined delay.

    You can find the source code of the `DSSSpell` contract here:

    Attributes:
        web3: An instance of `Web` from `web3.py`.
        address: Ethereum address of the `DSSSpell` contract.
    """

    # This ABI and BIN was used from the Mcd Ilk Line Spell
    # https://etherscan.io/address/0x3438Ae150d4De7F356251675B40B9863d4FD97F0
    abi = Contract._load_abi(__name__, 'abi/McdIlkLineSpell.abi')
    bin = Contract._load_bin(__name__, 'abi/McdIlkLineSpell.bin')

    def __init__(self, web3: Web3, address: Address):
        assert (isinstance(web3, Web3))
        assert (isinstance(address, Address))

        self.web3 = web3
        self.address = address
        self._contract = self._get_contract(web3, self.abi, address)

    def done(self) -> bool:
        return self._contract.call().done()

    def eta(self) -> datetime:
        try:
            timestamp = self._contract.call().eta()
        except ValueError:
            timestamp = 0

        return datetime.utcfromtimestamp(timestamp)

    def deploy(self, web3: Web3):
        return DSSSpell(web3=web3, address=Contract._deploy(web3, McdIlkLineSpell.abi, McdIlkLineSpell.bin, []))

    def schedule(self):
        return Transact(self, self.web3, self.abi, self.address, self._contract, 'schedule', [])

    def cast(self):
        return Transact(self, self.web3, self.abi, self.address, self._contract, 'cast', [])


class ChiefKeeper:
    """Keeper that lifts the hat and streamlines executive actions"""

    logger = logging.getLogger('chief-keeper')

    def __init__(self, args: list, **kwargs):
        """Pass in arguements assign necessary variables/objects and instantiate other Classes"""

        parser = argparse.ArgumentParser("chief-keeper")

        parser.add_argument("--rpc-host", type=str, default="localhost",
                            help="JSON-RPC host (default: `localhost')")

        parser.add_argument("--rpc-port", type=int, default=8545,
                            help="JSON-RPC port (default: `8545')")

        parser.add_argument("--rpc-timeout", type=int, default=10,
                            help="JSON-RPC timeout (in seconds, default: 10)")

        parser.add_argument("--network", type=str, required=True,
                            help="Network that you're running the Keeper on (options, 'mainnet', 'kovan', 'testnet')")

        parser.add_argument("--eth-from", type=str, required=True,
                            help="Ethereum address from which to send transactions; checksummed (e.g. '0x12AebC')")

        parser.add_argument("--eth-key", type=str, nargs='*',
                            help="Ethereum private key(s) to use (e.g. 'key_file=/path/to/keystore.json,pass_file=/path/to/passphrase.txt')")

        parser.add_argument("--dss-deployment-file", type=str, required=False,
                            help="Json description of all the system addresses (e.g. /Full/Path/To/configFile.json)")

        parser.add_argument("--chief-deployment-block", type=int, required=False, default=0,
                            help=" Block that the Chief from dss-deployment-file was deployed at (e.g. 8836668")

        parser.add_argument("--max-errors", type=int, default=100,
                            help="Maximum number of allowed errors before the keeper terminates (default: 100)")

        parser.add_argument("--debug", dest='debug', action='store_true',
                            help="Enable debug output")

        parser.set_defaults(cageFacilitated=False)
        self.arguments = parser.parse_args(args)

        self.web3 = kwargs['web3'] if 'web3' in kwargs else Web3(HTTPProvider(endpoint_uri=f"https://{self.arguments.rpc_host}:{self.arguments.rpc_port}",
                                                                              request_kwargs={"timeout": self.arguments.rpc_timeout}))
        self.web3.eth.defaultAccount = self.arguments.eth_from
        register_keys(self.web3, self.arguments.eth_key)
        self.our_address = Address(self.arguments.eth_from)

        if self.arguments.dss_deployment_file:
            self.dss = DssDeployment.from_json(web3=self.web3, conf=open(self.arguments.dss_deployment_file, "r").read())
        else:
            self.dss = DssDeployment.from_network(web3=self.web3, network=self.arguments.network)

        self.deployment_block = self.arguments.chief_deployment_block

        self.max_errors = self.arguments.max_errors
        self.errors = 0

        self.confirmations = 0


        logging.basicConfig(format='%(asctime)-15s %(levelname)-8s %(message)s',
                            level=(logging.DEBUG if self.arguments.debug else logging.INFO))


    def main(self):
        """ Initialize the lifecycle and enter into the Keeper Lifecycle controller

        Each function supplied by the lifecycle will accept a callback function that will be executed.
        The lifecycle.on_block() function will enter into an infinite loop, but will gracefully shutdown
        if it recieves a SIGINT/SIGTERM signal.

        """

        with Lifecycle(self.web3) as lifecycle:
            self.lifecycle = lifecycle
            lifecycle.on_startup(self.check_deployment)
            lifecycle.on_block(self.process_block)


    def check_deployment(self):
        self.logger.info('')
        self.logger.info('Please confirm the deployment details')
        self.logger.info(f'Keeper Balance: {self.web3.eth.getBalance(self.our_address.address) / (10**18)} ETH')
        self.logger.info(f'DS-Chief: {self.dss.ds_chief.address}')
        self.logger.info(f'DS-Pause: {self.dss.pause.address}')
        self.logger.info('')
        self.initial_query()


    def initial_query(self):
        self.logger.info('')
        self.logger.info('Querying Yays in DS-Chief since last update ( ! Could take up to 15 minutes ! )')

        basepath = os.path.dirname(__file__)
        filepath = os.path.abspath(os.path.join(basepath, "db_"+self.arguments.network+".json"))

        if os.path.isfile(filepath) and os.access(filepath, os.R_OK):
        # checks if file exists
            self.logger.info("Simple database exists and is readable")
            self.db = TinyDB(filepath)
        else:
            self.logger.info("Either file is missing or is not readable, creating simple database")
            self.db = TinyDB(filepath)

            blockNumber = self.web3.eth.blockNumber
            self.db.insert({'last_block_checked_for_yays': blockNumber})

            yays = self.get_yays(self.deployment_block, blockNumber)
            self.db.insert({'yays': yays})

            etas = self.get_etas(yays, blockNumber)
            self.db.insert({'upcoming_etas': etas})


    def process_block(self):
        """Callback called on each new block. If too many errors, terminate the keeper to minimize potential damage."""
        if self.errors >= self.max_errors:
            self.lifecycle.terminate()
        else:
            self.check_hat()
            self.check_eta()


    def check_eta(self):
        blockNumber = self.web3.eth.blockNumber
        now = self.web3.eth.getBlock(blockNumber).timestamp
        self.logger.info(f'Checking scheduled spells on block {blockNumber}')

        self.update_db_etas(blocknumber)
        etas = self.db.get(doc_id=3)["upcoming_etas"]

        yays = list(etas.keys())

        for yay in yays:
            if etas[yay] < now:
                spell = DSSSpell(self.web3, Address(yay))

                if spell.done() == False:
                    spell.cast().transact(gas_price=self.gas_price())

                del etas[key]

        self.db.update({'etas': etas}, doc_ids=[3])




    def check_hat(self):
        blockNumber = self.web3.eth.blockNumber
        self.logger.info(f'Checking Hat on block {blockNumber}')

        self.update_db_yays(blockNumber)
        yays = self.db.get(doc_id=2)["yays"]

        hat = self.dss.ds_chief.get_hat().address
        hatApprovals = self.dss.ds_chief.get_approvals(hat)

        contender, highestApprovals = hat, hatApprovals

        for yay in yays:
            contenderApprovals = self.dss.ds_chief.get_approvals(yay)
            if contenderApprovals > highestApprovals:
                contender = yay
                highestApprovals = contenderApprovals

        if contender != hat:
            self.logger.info(f'Lifting hat')
            self.logger.info(f'Old hat ({hat}) with Approvals {hatApprovals}')
            self.logger.info(f'New hat ({contender}) with Approvals {highestApprovals}')
            self.dss.ds_chief.lift(Address(contender)).transact(gas_price=self.gas_price())
            spell = DSSSpell(self.web3, Address(contender))

            if spell.done() == False:
                eta = self.get_eta_inUnix(spell)
                now = self.web3.eth.getBlock(blockNumber).timestamp

                if eta == 0:
                    spell.schedule().transact(gas_price=self.gas_price())

        else:
            self.logger.info(f'Current hat ({hat}) with Approvals {hatApprovals}')


    def get_eta_inUnix(self, spell: DSSSpell):
        eta = spell.eta()
        etaInUnix = eta.replace(tzinfo=timezone.utc).timestamp()

        return etaInUnix


    def update_db_etas(self, blockNumber: int):
        """ Add yays with etas that have yet to be passed """
        yays = self.db.get(doc_id=2)["yays"]
        etas = get_etas(yays, blockNumber)

        self.db.update({'upcoming_etas': etas}, doc_ids=[3])



    def get_etas(self, yays, blockNumber: int):
        """ Get all etas that are scheduled in the future """
        etas = {}
        for yay in yays:

            #Check if yay is an address to an EOA or a contract
            if is_contract_at(self.web3, Address(yay)):
                spell = DSSSpell(self.web3, Address(yay))
                eta = self.get_eta_inUnix(spell)

                if eta > self.web3.eth.getBlock(blockNumber).timestamp:
                    etas[spell.address] = eta

        return etas


    def update_db_yays(self, currentBlockNumber: int):

        DBblockNumber = self.db.get(doc_id=1)["last_block_checked_for_yays"]
        newYays = self.get_yays(DBblockNumber,currentBlockNumber)
        oldYays = self.db.get(doc_id=2)["yays"]

        self.db.update({'yays': oldYays + newYays}, doc_ids=[2])
        self.db.update({'last_block_checked_for_yays': currentBlockNumber}, doc_ids=[1])





    def get_yays(self, beginBlock: int, endBlock: int):

        etches = self.dss.ds_chief.past_etch_in_range(beginBlock, endBlock)
        maxYays = self.dss.ds_chief.get_max_yays()

        yays = []
        for etch in etches:
            yays = yays + self.unpack_slate(etch.slate, maxYays)

        yays = list(dict.fromkeys(yays))

        return yays if not None else []

    # inspiration -> https://github.com/makerdao/dai-plugin-governance/blob/master/src/ChiefService.js#L153
    # Fix
    # def unpack_slate(self, slate, i = 0):
    #     try:
    #         return [self.dss.ds_chief.get_yay(slate, i)].extend(
    #             self.unpack_slate(slate, i + 1))
    #     except:
    #         return []

    def unpack_slate(self, slate, maxYays: int):
        yays = []

        for i in range(0, maxYays):
            try:
                yay = [self.dss.ds_chief.get_yay(slate,i)]
            except ValueError:
                break
            yays = yays + yay

        return yays




    def gas_price(self):
        """  DefaultGasPrice """
        return DefaultGasPrice()


if __name__ == '__main__':
    ChiefKeeper(sys.argv[1:]).main()
