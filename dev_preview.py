"""Local development entry point; honors preview runner host and port flags."""
import argparse
import os
p=argparse.ArgumentParser()
p.add_argument('--host',default='127.0.0.1')
p.add_argument('--port',type=int,default=8094)
p.add_argument('--strictPort',action='store_true')
a=p.parse_args()
os.environ['FT_BIND']=a.host
os.environ['FT_PORT']=str(a.port)
os.environ.setdefault('FT_DIR','/tmp/dildo-preview')
import ft_node
ft_node.main()
