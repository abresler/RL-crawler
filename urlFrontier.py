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
from node_globals import *


# url frontier object at a node #[nodeN] of [numNodes]
#
# Primary external routines:
#
# - For CrawlThread:
#   *  get_crawl_task()
#   *  log_and_add_extracted(host_addr, success, time_taken, urls)
#
# - For MaintenanceThread:
#   *  clean_and_fill()
#
# - For initialization (sole) thread:
#   *  initialize(urls)

class urlFrontier:
  
  def __init__(self, node_n, num_nodes, num_threads, seen_persist, Q_logs=None):
    self.node_n = node_n
    self.num_nodes = num_nodes
    self.num_threads = num_threads
    self.Q_logs = Q_logs
    
    # crawl task Queue
    # Priority Queue ~ [ (next_pull_time, host_addr, url, ref_page_stats, seed_dist) ]
    self.Q_crawl_tasks = Queue.PriorityQueue()

    # host queue dict
    # { host_addr: [(url, ref_page_stats, seed_dist), ...] }
    self.hqs = {}
    
    # seen url check
    # Bloom Filter ~ [ url ]
    if seen_persist:
      try:
        self.seen = BloomFilter.open(BF_FILENAME)
      except:
        self.Q_logs.put('Error opening bloom filter, creating new one')
        self.seen = BloomFilter(BF_CAPACITY, BF_ERROR_RATE, BF_FILENAME)
    else:
      self.seen = BloomFilter(BF_CAPACITY, BF_ERROR_RATE, BF_FILENAME)

    # DNS Cache
    # { netloc: (host_addr, time_last_checked) }
    self.DNScache = {}

    # overflow url Queue
    # Queue ~ [ (host_addr, url, ref_page_stats, seen_dist) ]
    self.Q_overflow_urls = Queue.Queue()

    # host queue cleanup Queue
    # Priority Queue ~ [ (time_to_delete, host_addr) ]
    self.Q_hq_cleanup = Queue.PriorityQueue()

    # active url count queue- for counting/tracking active
    # Queue ~ [ True ]
    self.Q_active_count = Queue.Queue()

    # thread active url dict- a dict of active urls by thread using, for restart dump
    # { thread_name: active_url }
    # NOTE: note that there are problems with this methodology, but that errors will only lead
    # to data redundancy (as opposed to omission)...
    self.thread_active = {}
  

  # primary routine for getting a crawl task from queue
  def get_crawl_task(self):
    return self.Q_crawl_tasks.get()
  

  # primary routine to log crawl task done & submit extracted urls
  def log_and_add_extracted(self, host_addr, host_seed_dist, success, time_taken=0,url_pkgs=[]):

    # add urls to either hq of host_addr or else overflow queue
    for url_pkg in url_pkgs:
      self._add_extracted_url(host_addr, host_seed_dist, url_pkg)

    # handle failure of page pull
    # NOTE: TO-DO!
    if not success:
      pass

    # calculate time delay based on success
    now = datetime.datetime.now()
    r = random.random()
    td = 10*time_taken + r*BASE_PULL_DELAY if success else (1 + r)*BASE_PULL_DELAY
    next_time = now + datetime.timedelta(0, td)

    # if the hq of host_addr is not empty, enter new task in crawl task queue
    if len(self.hqs[host_addr]) > 0:

      # add task to crawl task queue
      r = self.hqs[host_addr].pop()
      self.Q_crawl_tasks.put((next_time, host_addr) + r)

    # else if empty, add task to cleanup queue
    else:
      self.Q_hq_cleanup.put((next_time, host_addr))
    
    # report crawl task done to queue, HOWEVER do not submit as done till payload dropped
    self.Q_crawl_tasks.task_done()


  # subroutine to add a url extracted from a host_addr
  def _add_extracted_url(self, ref_host_addr, ref_seed_dist, url_pkg):
    url_in, ref_page_stats = url_pkg
  
    # basic cleaning operations on url
    # NOTE: it is the responsibility of the crawlNode.py extract_links fn to server proper url
    url = re.sub(r'/$', '', url_in)

    # check if url already seen
    if url not in self.seen:

      # get host IP address of url
      url_parts = urlparse.urlsplit(url)
      host_addr = self._get_and_log_addr(url_parts.netloc)

      if host_addr is not None:

        # if this is an internal link, send directly to the serving hq
        # NOTE: need to check that equality operator is sufficient here!
        if host_addr == ref_host_addr:
          self.hqs[host_addr].append((url, ref_page_stats, ref_seed_dist))

          # !log as seen & add to active count
          self.seen.add(url)
          self.Q_active_count.put(True)
          
          if DEBUG_MODE:
            self.Q_logs.put("Active count: %s" % self.Q_active_count.qsize())
        
        else:

          # first make sure that this url does not exceed max link distance from seed
          if ref_seed_dist < MAX_SEED_DIST or MAX_SEED_DIST == -1:
          
            # check if this address belongs to this node
            url_node = hash(host_addr) % self.num_nodes
            if url_node == self.node_n:

              # add to overflow queue
              self.Q_overflow_urls.put((host_addr, url, ref_page_stats, ref_seed_dist + 1))

              # !log as seen & add to active count
              self.seen.add(url)
              self.Q_active_count.put(True)

              if DEBUG_MODE:
                self.Q_logs.put("Active count: %s" % self.Q_active_count.qsize())
            
            # else pass along to appropriate node
            # NOTE: TO-DO!
            else:
              pass

      # else if DNS was not resolved
      # NOTE: TO-DO!
      else:
        pass


  # subfunction for getting IP address either from DNS cache or web
  def _get_and_log_addr(self, hostname):
    
    # try looking up hostname in DNScache
    now = datetime.datetime.now()
    if self.DNScache.has_key(hostname):

      # check time for DNS refresh
      addr, created = self.DNScache[hostname]
      age = now - created
      if age.seconds > DNS_REFRESH_TIME:
        addr = self._get_addr(hostname)
        if addr is not None:
          self.DNScache[hostname] = (addr, now)
        else:
          del self.DNScache[hostname]
    else:
      addr = self._get_addr(hostname)
      if addr is not None:
        self.DNScache[hostname] = (addr, now)
    return addr
  

  # sub-subfunction for getting IP address from socket
  def _get_addr(self, hostname):
    try:
      addr_info = socket.getaddrinfo(hostname, None)
    except Exception as e:
      self.Q_logs.put('DNS ERROR: skipping ' + hostname)
      return None

    # ensure result is non-null
    if len(addr_info) > 0:
      return addr_info[0][4][0]
    else:
      self.Q_logs.put('DNS ERROR: skipping ' + hostname)
      return None


  # primary maintenance routine- clear one old queue and replace with a new one from overflow
  # NOTE: this assumes constant number of existing queues is always present
  def clean_and_fill(self):
    
    # get queue to delete & time to delete at
    time_to_delete, host_addr = self.Q_hq_cleanup.get()

    # wait to delete
    wait_time = time_to_delete - datetime.datetime.now()
    time.sleep(max(0, wait_time.total_seconds()))

    # delete queue and add new one
    del self.hqs[host_addr]
    added = False
    while not added:
      added = self._overflow_to_new_hq()

    # log task done to both queues
    self.Q_hq_cleanup.task_done()
    self.Q_overflow_urls.task_done()


  # subroutine for transferring urls from overflow queue to new hq
  def _overflow_to_new_hq(self):
    r = self.Q_overflow_urls.get()
    
    # if hq already exists, recycle- insertion not thread safe
    # NOTE: better way to do this while ensuring thread safety here?
    if self.hqs.has_key(host_addr):
      self.Q_overflow_urls.task_done()
      self.Q_overflow_urls.put(r)
      return False
    else:
      
      # create new empty hq and send seed url to crawl task queue
      self.hqs[r[0]] = []
      self.Q_crawl_tasks.put(r.insert(0, datetime.datetime.now()))
      return True
  

  # primary routine for initialization of url frontier / hqs
  # NOTE: !!! Assumed that this is sole thread running when executed, prior to crawl start
  def initialize(self, urls=[]):
    now = datetime.datetime.now()
    
    # initialize all hqs as either full & tasked or empty & to be deleted
    i = 0
    while len(self.hqs) < HQ_TO_THREAD_RATIO*self.num_threads:
      i += 1
      
      # expend all given urls
      if len(urls) > 0:
        self._init_add_url(urls.pop())

      # else add empty queues and mark to be cleared & replaced
      else:
        self.hqs[i] = []
        self.Q_hq_cleanup.put((now, i))

    # if there are urls left over, add to appropriate queues
    for url in urls:
      self._init_add_url(url)

  
  # subroutine for adding url to hq, assuming only one thread running (initialization)
  def _init_add_url(self, url_in):

    # basic cleaning operations on url
    url = re.sub(r'/$', '', url_in)

    # NOTE: do not check if seen, works for restart & assume original seed list is de-duped

    # get host IP address of url
    url_parts = urlparse.urlsplit(url)
    host_addr = self._get_and_log_addr(url_parts.netloc)

    if host_addr is not None:

      # check if this address belongs to this node
      url_node = hash(host_addr) % self.num_nodes
      if url_node == self.node_n:

        # !log as seen & add to active count
        self.seen.add(url)
        self.Q_active_count.put(True)

        if DEBUG_MODE:
          self.Q_logs.put("Active count: %s" % self.Q_active_count.qsize())

        # add to an existing hq, or create new one & log new crawl task, or add to overflow
        if self.hqs.has_key(host_addr):
          self.hqs[host_addr].append((url, None, 0))
        elif len(self.hqs) < HQ_TO_THREAD_RATIO*self.num_threads:
          self.hqs[host_addr] = []
          self.Q_crawl_tasks.put((datetime.datetime.now(), host_addr, url, None, 0))
        else:
          self.Q_overflow_urls.put((host_addr, url, None, 0))

      # else pass along to appropriate node
      # NOTE: TO-DO!
      else:
        pass

    # else if DNS was not resolved
    # NOTE: TO-DO!
    else:
      pass  


  # routine called on abort (by user interrupt or by MAX_CRAWLED count being reached) to
  # save current contents of all queues to disk & seen filter flushed for restart
  def dump_for_restart(self):
    
    # get all urls in Q_crawl_tasks, hqs, or Q_overflow_urls
    # only get urls as these will be re-injected through the initialize method of uf
    with open(RESTART_DUMP, 'w') as f:
      for thead_name, url in self.thread_active.iteritems():
        f.write(url + '\n')

      while self.Q_crawl_tasks.full():
        try:
          r = self.Q_crawl_tasks.get(False)
          f.write(r[2] + '\n')
        except:
          break

      for host_addr, paths in self.hqs.iteritems():
        for path in paths:
          f.write(path[0] + '\n')

      while self.Q_overflow_urls.full():
        try:
          r = self.Q_overflow_urls.get(False)
          f.write(r[1] + '\n')
        except:
          break

    # ensure seen filter file is synced
    self.seen.sync()

#
# --> Command line functionality
#
#if __name__ == '__main__':
#  if sys.argv[1] == 'test' and len(sys.argv) == 2:
#    full_test()
#  else:
#    print 'Usage: python urlFrontier.py ...'
#    print '(1) test'
