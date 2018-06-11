#!/usr/bin/env python2
# -*- coding: utf-8 -*-
"""
    Blockstack
    ~~~~~
    copyright: (c) 2014-2015 by Halfmoon Labs, Inc.
    copyright: (c) 2016 by Blockstack.org

    This file is part of Blockstack

    Blockstack is free software: you can redistribute it and/or modify
    it under the terms of the GNU General Public License as published by
    the Free Software Foundation, either version 3 of the License, or
    (at your option) any later version.

    Blockstack is distributed in the hope that it will be useful,
    but WITHOUT ANY WARRANTY; without even the implied warranty of
    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
    GNU General Public License for more details.
    You should have received a copy of the GNU General Public License
    along with Blockstack. If not, see <http://www.gnu.org/licenses/>.
""" 

import testlib
import virtualchain
import blockstack
import json

# activate tokens
"""
TEST ENV BLOCKSTACK_EPOCH_1_END_BLOCK 682
TEST ENV BLOCKSTACK_EPOCH_2_END_BLOCK 683
TEST ENV BLOCKSTACK_EPOCH_3_END_BLOCK 684
TEST ENV BLOCKSTACK_EPOCH_2_NAMESPACE_LIFETIME_MULTIPLIER 1
TEST ENV BLOCKSTACK_EPOCH_3_NAMESPACE_LIFETIME_MULTIPLIER 1
"""

wallets = [
    testlib.Wallet( "5JesPiN68qt44Hc2nT8qmyZ1JDwHebfoh9KQ52Lazb1m1LaKNj9", 100000000000 ),
    testlib.Wallet( "5KHqsiU9qa77frZb6hQy9ocV7Sus9RWJcQGYYBJJBb2Efj1o77e", 100000000000 ),
    testlib.Wallet( "5Kg5kJbQHvk1B64rJniEmgbD83FpZpbw2RjdAZEzTefs9ihN3Bz", 100000000000 ),
    testlib.Wallet( "5JuVsoS9NauksSkqEjbUZxWwgGDQbMwPsEfoRBSpLpgDX1RtLX7", 100000000000 ),
    testlib.Wallet( "5KEpiSRr1BrT8vRD7LKGCEmudokTh1iMHbiThMQpLdwBwhDJB1T", 100000000000 )
]

consensus = "17ac43c1d8549c3181b200f1bf97eb7d"
pk = None

def scenario( wallets, **kw ):
    global pk

    testlib.blockstack_namespace_preorder( "test", wallets[1].addr, wallets[0].privkey )
    testlib.next_block( **kw )

    testlib.blockstack_namespace_reveal( "test", wallets[1].addr, 52595, 250, 4, [6,6,6,6,6,6,0,0,0,0,0,0,0,0,0,0], 10, 10, wallets[0].privkey )
    testlib.next_block( **kw )

    testlib.blockstack_namespace_ready( "test", wallets[1].privkey )
    testlib.next_block( **kw )

    # pay for a name in a v1 namespace with Stacks
    pk = virtualchain.lib.ecdsalib.ecdsa_private_key().to_hex()
    addr = virtualchain.address_reencode(virtualchain.get_privkey_address(pk))

    # calculate the cost of doing so
    namespace = testlib.get_state_engine().get_namespace('test')
    stacks_price = blockstack.lib.scripts.price_name_stacks('foo', namespace, testlib.get_current_block(**kw))
    btc_price = blockstack.lib.scripts.price_name('foo', namespace, testlib.get_current_block(**kw))

    print ''
    print 'price of {} in Stacks is {}'.format('foo.test', stacks_price)
    print ''

    testlib.blockstack_send_tokens(addr, "STACKS", stacks_price * 3, wallets[0].privkey)
    testlib.send_funds(wallets[0].privkey, btc_price * 10, addr)    # fund with enough bitcoin
    testlib.next_block(**kw)

    # preorder/register using Stacks---Stacks should still be used since that's what the transaction indicates
    testlib.blockstack_name_preorder( "foo.test", pk, wallets[3].addr, price={'units': 'STACKS', 'amount': stacks_price})
    testlib.next_block( **kw )

    testlib.send_funds(wallets[0].privkey, btc_price * 10, addr)
    testlib.blockstack_name_register( "foo.test", pk, wallets[3].addr )
    testlib.next_block( **kw )

    # preorder/register using Bitcoin--Stacks should NOT be used since that's what the transaction indicates
    testlib.blockstack_name_preorder("bar.test", pk, wallets[3].addr, price={'units': 'BTC', 'amount': btc_price})
    testlib.next_block(**kw)

    testlib.blockstack_name_register('bar.test', pk, wallets[3].addr)
    testlib.next_block(**kw)

    balance_before = testlib.get_addr_balances(addr)[addr]['STACKS']

    # pay with both Stacks and Bitcoin.
    # will spend both Stacks and Bitcoin when we preorder
    res = testlib.blockstack_name_preorder('baz.test', pk, wallets[3].addr, price={'units': 'STACKS', 'amount': stacks_price}, tx_only=True, expect_success=True)
    txhex = res['transaction']
    tx = virtualchain.btc_tx_deserialize(txhex)

    # up the burn amount 
    btc_price = blockstack.lib.scripts.price_name('baz', namespace, testlib.get_current_block(**kw))
    tx['outs'][2]['script'] = virtualchain.btc_make_payment_script(blockstack.lib.config.BLOCKSTACK_BURN_ADDRESS)
    tx['outs'][2]['value'] = btc_price

    tx['outs'][1]['value'] -= btc_price

    # re-sign 
    for i in tx['ins']:
        i['script'] = ''

    txhex = virtualchain.btc_tx_serialize(tx)
    txhex_signed = virtualchain.tx_sign_all_unsigned_inputs(pk, testlib.get_utxos(addr), txhex)
    
    print txhex_signed

    res = testlib.broadcast_transaction(txhex_signed)
    if 'error' in res:
        print res
        return False

    testlib.next_block(**kw)

    # should have paid in Stacks
    balance_after = testlib.get_addr_balances(addr)[addr]['STACKS']
    if balance_after != balance_before - stacks_price:
        print 'baz.test cost {}'.format(balance_before - balance_after)
        return False

    testlib.blockstack_name_register('baz.test', pk, wallets[3].addr)
    testlib.next_block(**kw)

    balance_before = testlib.get_addr_balances(addr)[addr]['STACKS']

    # register a name where we pay not enough stacks, but enough bitcoin.  should still go through
    # should favor Bitcoin payment over Stacks payment, but we should still burn both Stacks and BTC
    res = testlib.blockstack_name_preorder('goo.test', pk, wallets[3].addr, price={'units': 'STACKS', 'amount': stacks_price-1}, tx_only=True, expect_success=True)
    txhex = res['transaction']
    tx = virtualchain.btc_tx_deserialize(txhex)

    # up the burn amount to the name price
    btc_price = blockstack.lib.scripts.price_name('goo', namespace, testlib.get_current_block(**kw))
    tx['outs'][2]['script'] = virtualchain.btc_make_payment_script(blockstack.lib.config.BLOCKSTACK_BURN_ADDRESS)
    tx['outs'][2]['value'] = btc_price

    tx['outs'][1]['value'] -= btc_price

    # re-sign 
    for i in tx['ins']:
        i['script'] = ''

    txhex = virtualchain.btc_tx_serialize(tx)
    txhex_signed = virtualchain.tx_sign_all_unsigned_inputs(pk, testlib.get_utxos(addr), txhex)
    
    print txhex_signed

    res = testlib.broadcast_transaction(txhex_signed)
    if 'error' in res:
        print res
        return False

    testlib.next_block(**kw)

    # should have paid in Stacks
    balance_after = testlib.get_addr_balances(addr)[addr]['STACKS']
    if balance_after != balance_before - stacks_price + 1:
        print 'goo.test paid {}'.format(balance_before - balance_after)
        return False

    testlib.blockstack_name_register('goo.test', pk, wallets[3].addr)
    testlib.next_block(**kw)

    # TODO: try one with not enough of either (should be rejected)

def check( state_engine ):

    # not revealed, but ready 
    ns = state_engine.get_namespace_reveal( "test" )
    if ns is not None:
        print "namespace reveal exists"
        return False 

    ns = state_engine.get_namespace( "test" )
    if ns is None:
        print "no namespace"
        return False 

    if ns['namespace_id'] != 'test':
        print "wrong namespace"
        return False 

    for name in ['foo.test', 'bar.test', 'baz.test', 'goo.test']:
        # not preordered
        addr = virtualchain.address_reencode(virtualchain.get_privkey_address(pk))
        preorder = state_engine.get_name_preorder( name, virtualchain.make_payment_script(addr), wallets[3].addr )
        if preorder is not None:
            print "preorder exists"
            return False
        
        # registered 
        name_rec = state_engine.get_name( name )
        if name_rec is None:
            print "name does not exist"
            return False 

        # owned by
        if name_rec['address'] != wallets[3].addr or name_rec['sender'] != virtualchain.make_payment_script(wallets[3].addr):
            print "sender is wrong"
            return False 

    # paid for foo.test and baz.test with Stacks
    # however, baz.test's burn output is equal to the bitcoin price
    for name in ['foo', 'baz']:
        name_rec = state_engine.get_name( name + '.test' )
        stacks_price = blockstack.lib.scripts.price_name_stacks(name, ns, state_engine.lastblock)
        if name_rec['token_fee'] != stacks_price:
            print 'paid wrong token fee for {}.test'.format(name)
            print 'expected {} ({}), got {} ({})'.format(stacks_price, type(stacks_price), name_rec['token_fee'], type(name_rec['token_fee']))
            return False

        if name == 'foo':
            if name_rec['op_fee'] > 5500:  # dust minimum
                print 'paid in BTC ({})'.format(name_rec['op_fee'])
                return False

        elif name == 'baz':
            if name_rec['op_fee'] != blockstack.lib.scripts.price_name(name, ns, state_engine.lastblock):
                print 'paid wrong BTC for baz.test ({})'.format(name_rec['op_fee'])
                return False

    for name in ['baz', 'goo']:
        name_rec = state_engine.get_name( name + '.test' )

        # make sure we debited Stacks in both cases
        if name == 'baz':
            if name_rec['token_fee'] != blockstack.lib.scripts.price_name_stacks(name, ns, state_engine.lastblock):
                print 'paid wrong token fee for {}.test'.format(name)
                print 'expected {}, got {}'.format(blockstack.lib.scripts.price_name_stacks(name, ns, state_engine.lastblock), name_rec['token_fee'])
                return False

        if name == 'goo':
            if name_rec['token_fee'] != blockstack.lib.scripts.price_name_stacks(name, ns, state_engine.lastblock) - 1:
                print 'paid wrong token fee for {}.test'.format(name)
                print 'expected {}, got {}'.format(blockstack.lib.scripts.price_name_stacks(name, ns, state_engine.lastblock) - 1, name_rec['token_fee'])
                return False

        if name_rec['op_fee'] != blockstack.lib.scripts.price_name(name, ns, state_engine.lastblock):
            print 'paid wrong BTC for {}.test ({})'.format(name, name_rec['op_fee'])
            return False

    return True
