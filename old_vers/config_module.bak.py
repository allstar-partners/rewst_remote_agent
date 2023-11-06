import json
import logging
import platform
import httpx
import socket
import asyncio
import uuid
import psutil
import os
import sys
import socket
import subprocess
from __version__ import __version__

# Put Timestamps on logging entries
logging.basicConfig(
    level=logging.INFO,  
    format='%(asctime)s %(levelname)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)

REQUIRED_KEYS = [
    'azure_iot_hub_host',
    'device_id',
    'shared_access_key',
    'rewst_engine_host',
    'rewst_org_id'
]


def get_config_file_path(org_id=None, config_file=None):
    if config_file:
        return config_file
    os_type = platform.system()
    if os_type == "Windows":
        config_dir = os.path.join(os.environ.get('PROGRAMDATA'), 'RewstRemoteAgent', org_id if org_id else '')
    elif os_type == "Linux":
        config_dir = f"/etc/rewst_remote_agent/{org_id}"
    elif os_type == "Darwin":
        config_dir = os.path.expanduser(f"~/Library/Application Support/RewstRemoteAgent/{org_id}")
    
    if not os.path.exists(config_dir):
        try:
            if not os.path.exists(config_dir):
                os.makedirs(config_dir)
        except Exception as e:
            logging.error(f"Failed to create directory {config_dir}: {str(e)}")
            raise 
    
    config_file_path = os.path.join(config_dir, "config.json")
    logging.info(f"Config File Path: {config_file_path}")
    return config_file_path


def save_configuration(config_data, config_file=None):
    org_id = config_data["rewst_org_id"]
    config_file_path = get_config_file_path(org_id, config_file)
    with open(config_file_path, 'w') as f:
        json.dump(config_data, f, indent=4)


def load_configuration(org_id=None, config_file=None):
    config_file_path = get_config_file_path(org_id, config_file)
    try:
        with open(config_file_path) as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    

async def fetch_configuration(config_url, secret=None):
    # Collect host information
    host_info = {
        "agent_version": __version__,
        "executable_path": sys.executable,
        "hostname": socket.gethostname(),
        "mac_address": get_mac_address(),
        "operating_system": platform.platform(),
        "cpu_model": platform.processor(),
        "ram_gb": psutil.virtual_memory().total / (1024 ** 3),
        "is_ad_domain_controller": is_domain_controller(),
        "is_entra_connect_server": is_entra_connect_server(),
        "ad_domain": get_ad_domain_name(),
        "entra_domain": get_entra_domain()
    }

    headers = {}
    if secret:
        headers['x-rewst-secret'] = secret
    
    retry_intervals = [(5, 12), (60, 60), (300, float('inf'))]  # (interval, max_retries) for each phase
    for interval, max_retries in retry_intervals:
        retries = 0
        while retries < max_retries:
            retries += 1
            async with httpx.AsyncClient(timeout=None) as client:  # Set timeout to None to wait indefinitely
                try:
                    response = await client.post(
                        config_url,
                        json=host_info,
                        headers=headers,
                        follow_redirects=True
                    )
                except httpx.TimeoutException:
                    logging.warning(f"Attempt {retries}: Request timed out. Retrying...")
                    continue  # Skip the rest of the loop and retry

                except httpx.RequestError as e:
                    logging.warning(f"Attempt {retries}: Network error: {e}. Retrying...")
                    continue

                if response.status_code == 303:
                    logging.info("Waiting while Rewst processes Agent Registration...")  # Custom message for 303
                elif response.status_code == 200:
                    data = response.json()
                    config_data = data.get('configuration')
                    if config_data and all(key in config_data for key in REQUIRED_KEYS):
                        return config_data
                    else:
                        logging.warning(f"Attempt {retries}: Missing required keys in configuration data. Retrying...")
                elif response.status_code == 400 or response.status_code == 401:
                    logging.error(f"Attempt {retries}: Not authorized. Check your config secret.")
                else:
                    logging.warning(f"Attempt {retries}: Received status code {response.status_code}. Retrying...")

            await asyncio.sleep(interval)

        logging.info(f"Moving to next retry phase: {interval}s interval for {max_retries} retries.")
    logging.INFO("This process will end when the service is installed.")

 
def get_mac_address():
    # Returns the MAC address of the host without colons
    mac_num = hex(uuid.UUID(int=uuid.getnode()).int)[2:]
    mac_address = ':'.join(mac_num[i: i + 2] for i in range(0, 11, 2))
    return mac_address.replace(':', '')


def is_domain_controller():
    if platform.system().lower() != 'windows':
        return False
    domain_name = get_ad_domain_name()
    if domain_name is None:
        logging.warning("Could not determine domain name.")
        return False
    try:
        result = subprocess.run([f'nltest', f'/dclist:{domain_name}'], text=True, capture_output=True, check=True)
        domain_controllers = result.stdout.split('\n')
        local_machine = socket.gethostname()
        return any(local_machine in dc for dc in domain_controllers)
    except subprocess.CalledProcessError as e:
        logging.error(f"Command failed with error: {str(e)}")
        return False
    except Exception as e:
        logging.error(f"An unexpected error occurred: {str(e)}")
        return False


def get_ad_domain_name():
    if platform.system().lower() != 'windows':
        return None
    try:
        result = subprocess.run(['dsregcmd', '/status'], text=True, capture_output=True, check=True)
        for line in result.stdout.split('\n'):
            if 'Domain Name' in line:
                return line.split(':')[1].strip()
        logging.warning("Domain Name not found in dsregcmd output.")
        return None
    except subprocess.CalledProcessError as e:
        logging.error(f"Command failed with error: {str(e)}")
        return None
    except Exception as e:
        logging.error(f"An unexpected error occurred: {str(e)}")
        return None


def get_entra_domain():
    try:
        result = subprocess.run(['dsregcmd', '/status'], text=True, capture_output=True)
        output = result.stdout
        for line in output.splitlines():
            if 'AzureAdJoined' in line and 'YES' in line:
                for line in output.splitlines():
                    if 'DomainName' in line:
                        domain_name = line.split(':')[1].strip()
                        return domain_name
    except Exception as e:
        logging.warning(f"Unexpected issue querying for Entra Domain: {str(e)}")
        pass  # Handle exception if necessary
    return None


def is_entra_connect_server():
    if platform.system().lower() != 'windows':
        return False
    potential_service_names = ["ADSync", "Azure AD Sync", "EntraConnectSync", "OtherFutureName"]
    for service_name in potential_service_names:
        if is_service_running(service_name):
            return True
    return False


def is_service_running(service_name):
    for service in psutil.win_service_iter() if platform.system() == 'Windows' else psutil.process_iter(['name']):
        if service.name().lower() == service_name.lower():
            return True
    return False