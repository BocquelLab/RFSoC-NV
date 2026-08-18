import sys
if len(sys.argv) != 2:
    print("Provide the IP as an argument")
    exit()

import qick 
print(qick.__dir__())
from qick import QickSoc, QickConfig

qick_path = "/home/xilinx/jupyter_notebooks/qick-dawg-8b8cbe7/firmware/photon_counting/qick_4x2.bit"
ns_host = sys.argv[1]

import Pyro4

ns_port = 8888

server_name = "myqick"

print("looking for nameserver . . .")
Pyro4.config.REQUIRE_EXPOSE = False
Pyro4.config.SERIALIZER = "pickle"
Pyro4.config.SERIALIZERS_ACCEPTED=set(['pickle'])
Pyro4.config.PICKLE_PROTOCOL_VERSION=4
ns = Pyro4.locateNS(host=ns_host, port=ns_port)
print("found nameserver")

# if we have multiple network interfaces, we want to register the daemon using the IP address that faces the nameserver
host = Pyro4.socketutil.getInterfaceAddress(ns._pyroUri.host)
daemon = Pyro4.Daemon(host=host)

# if you want to use a different firmware image or set some initialization options, you would do that here
soc = QickSoc(bitfile=qick_path, force_init_clks=True)
print("initialized QICK")
print(QickConfig(soc.get_cfg()))
# register the QickSoc in the daemon (so the daemon exposes the QickSoc over Pyro4)
# and in the nameserver (so the client can find the QickSoc)
ns.register(server_name, daemon.register(soc))
print("registered QICK")

# register in the daemon all the objects we expose as properties of the QickSoc
# we don't register them in the nameserver, since they are only meant to be accessed through the QickSoc proxy
# https://pyro4.readthedocs.io/en/stable/servercode.html#autoproxying
# https://github.com/irmen/Pyro4/blob/master/examples/autoproxy/server.py
for obj in soc.autoproxy:
    daemon.register(obj)
    print("registered member "+str(obj))
    
print("starting daemon")
daemon.requestLoop() # this will run forever until interrupted
