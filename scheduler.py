"""

"""
import argparse
import yaml
from datetime import datetime
import requests

def read_yaml(filepath):
    with open(filepath, 'r') as stream:
        parsed_yaml=yaml.safe_load(stream)
        return parsed_yaml
    return {}

def parse_args():
    parser = argparse.ArgumentParser(description="Tool to create docker network schedule")
    parser.add_argument( '--path',help='Location of scheduler')
    return parser.parse_args()


    

def send_actions(host,container_names,url):
    host_name=host[0]
    host_id=container_names[host_name]
    actions=host[1]['action']
    result=""
    if actions!=None:
        for action in actions:
            
            if len(result)==0:
                result=action['name']+"="+action['value']
            else:
                result=result+"&"+action['name']+"="+action['value']

    if len(result)>0:
        response=requests.post(url,data=result)
        print(response)


def get_all_container_names(url):
    container_names_dict={}
    all_names= requests.request('LIST',url).content.decode().split("\n")
    for name in [name for name  in all_names if "id=" in name]:
        name=name.replace("# ","")
        args=name.split(" ")
        id=args[0].split("=")[1]
        name=args[1].split("=")[1]
        container_names_dict[name]=id
    return container_names_dict

def main():

    # Assigning arguments
    args    = parse_args()
    path    = args.path
    schedule=read_yaml(path)
    
    #
    duration=schedule["schedule"]["settings"]["duration"]
    ip=schedule["schedule"]["settings"]["ip"]
    port=schedule["schedule"]["settings"]["port"]
    container_names=get_all_container_names("http://"+str(ip)+":"+str(port))

    hosts=schedule["schedule"]["hosts"].items()
    hosts_flat=[]

    for host in hosts:
        for time_slice in host[1]:
            hosts_flat.append((host[0],time_slice))
    hosts_flat=sorted(hosts_flat,key=lambda x:int(x[1]['start']))

    start_time= datetime.utcnow()
    print("Will start sending changes to network status at: ",)
    for host in hosts_flat:
        print("Current host:",host)
        
        wait_until_time=False
        while not wait_until_time:
            delta=(start_time.utcnow()-start_time).total_seconds()
            host_start=int(host[1]['start'])
            if delta>host_start:
                send_actions(host,container_names,"http://"+str(ip)+":"+str(port))
                wait_until_time=True

    print("Will stop after: ",duration-(start_time.utcnow()-start_time).total_seconds(),"seconds")
    while (start_time.utcnow()-start_time).total_seconds()<=duration:
        pass              
    print("Done")

if __name__ == '__main__':
    main()
