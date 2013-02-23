import sys
import urlparse
import socket
import heapq
import hashlib
import datetime
import random
from util import *
import Queue
import re
from pybloomfilter import BloomFilter


# NOTE NOTE --> Overall to-do list
# - some sort of function to handle transferring e.g. DNS cache data to disk when/if too large
# - way to send extracted urls that do not belong to this node to other node in periodic packet
#   to avoid index muddling/confusion
# - find out what the server footprint of socket is...
# - unified error system... read up on pythonic ways of doing this
# - robots.txt reader / policy system (ask Matt about this...)
# - what happens if a crawl thread calls 'get' but fails before calling 'log'?
# - **issue: different netlocs corresponding to same server get hashed out to different
#   nodes!!  e.g. hello.crecomparex.com and www.crecomparex.com...
# - handle logging/possible re-try of pages that failed to pull... ALSO: detecting whether
#   entire server might be down, putting url back and putting a long wait time in backq_heap
# - implement fingerprinting for deduplication?


# NOTE NOTE --> Notes on areas to look for efficiency gain (time and/or space)
# - datetime objects: more efficent way to store this (time.time?)
# - clean up empty backq objects (queue & associated table entry)?
# - try a Trie structure instead of a Bloom filter for seen lookup?  Or try just a Bloom of
#   the hostname, followed by a simple list/dict lookup of the relative path? --> NO to this
#   second idea, might as well just use a dict lookup then...


# global time constants
DNS_REFRESH_TIME = 21600  # Refresh DNS every 6 hours
BASE_PULL_DELAY = 60  # Base time constant to wait for pulling from domain = 60 secs


# Bloom filter constants
BF_CAPACITY = 10000000
BF_ERROR_RATE = 0.001
BF_FILENAME = 'seen.bloom'


# url frontier object at a node #[nodeN] of [numNodes]
# primary interface methods for a crawl thread are:
#  * add(url)
#  * get() --> addr, url, next_pull_time
#  * log(addr, pulled, time_to_pull)
class urlFrontier:
  
  def __init__(self, node_n, num_nodes):
    self.node_n = node_n
    self.num_nodes = num_nodes
    
    # Priority Queue ~ [ (next_pull_time, addr) ]
    self.backq_heap = Queue.PriorityQueue()

    # { addr: { 'last_pulled': time_last_pulled, 'backq': [url, ...] }
    self.backq_table = {}

    # { url: BOOL }
    self.seen = BloomFilter(BF_CAPACITY, BF_ERROR_RATE, BF_FILENAME)

    # { hostname: (addr, time_last_checked) }
    self.DNScache = {}
  


  # primary routine for adding an extracted url to the urlFrontier 
  def add(self, url_in):
    
    # basic cleaning operations on url
    url = re.sub(r'/$', '', url_in)
    
    # split url into parts
    url_parts = urlparse.urlsplit(url)
    
    # check if url belongs to this node
    url_node = hash(url_parts.netloc) % self.num_nodes
    if url_node == self.node_n:

      # get (and log) url IP address
      addr = self.get_log_addr(url_parts.netloc)
      if addr is not None:

        # check against Bloom filter to make sure the url has not been seen before
        if url not in self.seen:
          self.add_to_backq(addr, url)

    # else if the extracted link belongs to another node, package to be sent
    # NOTE: TO-DO... should send in packets periodically to avoid too much inter-node traffic
    else:
      pass


  # subfunction for getting IP address either from DNS cache or web
  def get_log_addr(self, hostname):
    
    # try looking up hostname in DNScache
    now = datetime.datetime.now()
    if self.DNScache.has_key(hostname):

      # check time for DNS refresh
      addr, created = self.DNScache[hostname]
      age = now - created
      if age.seconds > DNS_REFRESH_TIME:
        addr = self.get_addr(hostname)
        if addr is not None:
          self.DNScache[hostname] = (addr, now)
        else:
          del self.DNScache[hostname]
    else:
      addr = self.get_addr(hostname)
      if addr is not None:
        self.DNScache[hostname] = (addr, now)
    return addr
  

  # sub-subfunction for getting IP address from socket
  def get_addr(self, hostname):
    try:
      addr_info = socket.getaddrinfo(hostname, None)
    except Exception as e:
      print 'DNS Error accessing ' + hostname
      return None

    # ensure result is non-null
    if len(addr_info) > 0:
      return addr_info[0][4][0]
    else:
      print 'DNS Error, null returned for ' + hostname
      return None


  # subfunction for inserting unseen, un-queued url into back queue
  def add_to_backq(self, addr, url):
    now = datetime.datetime.now()

    # check to see if a queue for the ip address exists yet
    if self.backq_table.has_key(addr):

      # check if queue is empty: if so push new entry to heap
      if len(self.backq_table[addr]['backq']) == 0:
        next_pull_time = self.backq_table[addr]['last_pulled'] + datetime.timedelta(0, BASE_PULL_DELAY + random.random()*60)
        self.backq_heap.put((next_pull_time, addr))
        print 'Q + (%s, %s)' % (next_pull_time, addr)

      # now add full url to appropriate back queue
      self.backq_table[addr]['backq'].append(url)

    # else if no back queue entry exists yet
    else:

      # create back queue
      self.backq_table[addr] = {'last_pulled': None, 'backq': [url]}

      # add entry to heap
      self.backq_heap.put((now, addr))
      print 'Q + (%s, %s)' % (now, addr)

    # add to seen dict
    self.seen.add(url)
    return True


  # primary routine for requesting a url to crawl/parse
  def get(self):

    # pop back queue heap- note no timeout, should be handled by Queue/join
    next_pull_time, addr = self.backq_heap.get()

    # pop back queue
    url = self.backq_table[addr]['backq'].pop()
    return (addr, url, next_pull_time)


  # primary routine for logging that a url was successfully (or not) crawled
  def log(self, addr, pulled, time_to_pull=0):
    now = datetime.datetime.now()
    
    # handle case where page was pulled succesfully
    if pulled:
      next_pull_time = now + datetime.timedelta(0, 10*time_to_pull + 2*random.random()*BASE_PULL_DELAY)

    # handle case where there was a problem pulling the page- use default delay
    else:
      next_pull_time = now + datetime.timedelta(0, BASE_PULL_DELAY + random.random()*60)

      # NOTE: TO-DO: do something about page re-try / server down detection!

    # if there are still entries in the back queue, add entry to heap
    if len(self.backq_table[addr]['backq']) > 0:
      self.backq_heap.put((next_pull_time, addr))
      print 'Q + (%s, %s)' % (next_pull_time, addr)

    # else if back queue is empty, log time of pull
    else:
      self.backq_table[addr]['last_pulled'] = now


#
# --> UNIT TESTS
#

def full_test():
  print '\nTesting full url frontier system for single node...'
  uf = urlFrontier(0, 1)

  # test DNS get / cache subfunctions (get_log_addr, get_addr)
  print '\n(I) Testing DNS get/cache:'
  print '\n(1) uf.get_log_addr("www.crecomparex.com")'
  print datetime.datetime.now()
  with Timer() as t:
    addr = uf.get_log_addr("www.crecomparex.com")
  print 'IP address retrieved as ' + str(addr) + ' in ' + str(t.duration) + ' secs.'
  print 'IP address cached as ' + uf.DNScache['www.crecomparex.com'][0]
  print '\n(2) uf.get_log_addr("www.crecomparex.com")'
  print datetime.datetime.now()
  with Timer() as t:
    addr = uf.get_log_addr("www.crecomparex.com")
  print '2nd attempt: IP address retrieved as '+str(addr)+' in ' + str(t.duration) + ' secs.'

  # test add overall function (add, add_to_backq)
  print '\n(II) Testing overall add functionality:'
  test_add(uf, 'http://www.crecomparex.com')
  test_add(uf, 'http://www.crecomparex.com')
  test_add(uf, 'http://www.crecomparex.com/about.php')
  test_add(uf, 'http://www.crecomparex.com/terms.php')

  # test get / log overall function (get, log)
  print '\n(III) Testing overall get / log functionality:'
  for i in range(4):
    test_get_and_log(uf)

  # test add again after queue emptied
  print '\n(IV) Testing add to emptied queue'
  test_add(uf, 'http://www.crecomparex.com/contact.php')

  print '\n'



def test_add(uf, url):
  
  # add
  print '\n(*) uf.add("' + url + '")'
  print datetime.datetime.now()
  with Timer() as t:
    uf.add(url)
  print 'Completed in ' + str(t.duration) + ' secs' 
  print 'Printing backq_table:'
  print uf.backq_table
  print 'Printing backq_heap:'
  print uf.backq_heap



def test_get_and_log(uf):
  
  # get
  print '\n(*) uf.get()'
  print datetime.datetime.now()
  with Timer() as t:
    addr, url, next_get_time = uf.get()
  if addr is not None:
    print 'Got ' + addr + ', '+url+', '+str(next_get_time)+' in ' + str(t.duration) + ' secs' 
    print 'Printing backq_table:'
    print uf.backq_table
    print 'Printing backq_heap:'
    print uf.backq_heap

    # log
    print '\n(*) uf.log(addr, pulled, time_to_pull)'
    print datetime.datetime.now()
    with Timer() as t:
      uf.log(addr, True, random.random()*1.0)
    print 'Completed in ' + str(t.duration) + ' secs' 
    print 'Printing backq_table:'
    print uf.backq_table
    print 'Printing backq_heap:'
    print uf.backq_heap
  else:
    print 'None returned'
    print '\n(*) uf.log(None, False)'
    print datetime.datetime.now()
    with Timer() as t:
      uf.log(None, False)
    print 'Completed in ' + str(t.duration) + ' secs'



#
# --> Command line functionality
#
if __name__ == '__main__':
  if sys.argv[1] == 'test' and len(sys.argv) == 2:
    full_test()
  else:
    print 'Usage: python urlFrontier.py ...'
    print '(1) test'
