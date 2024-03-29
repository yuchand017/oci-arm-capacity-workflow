import io
import oci
import json
import logging
import dhooks
import asyncio
from datetime import datetime

from loghook.discord import DiscordHook

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log_hook = DiscordHook()

with open("config.json", "r", encoding="utf-8") as f:
    local_config = json.load(f)


oci_config = oci.config.from_file("./.oci/.config", "DEFAULT")

identity = oci.identity.IdentityClient(oci_config)
compute = oci.core.ComputeClient(oci_config)
virtual_network = oci.core.VirtualNetworkClient(oci_config)


async def exist_instance_shape(shape: str, /):
    instances = compute.list_instances(oci_config["tenancy"]).data
    for instance in instances:
        if instance.shape == shape:
            return True
    return False


async def create_compute_instance(
    compartment_id: str,
    availability_domain: str,
    display_name: str,
    shape: str,
    subnet_id: str,
    image_id: str,
    memory_in_gbs: float,
    ocpus: float,
    ssh_authorized_public_key: str,
):
    vnic_details = oci.core.models.CreateVnicDetails(
        assign_ipv6_ip=False,
        assign_public_ip=True,
        subnet_id=subnet_id,
        assign_private_dns_record=True,
    )
    create_instance_details = oci.core.models.LaunchInstanceDetails(
        compartment_id=compartment_id,
        availability_domain=availability_domain,
        display_name=display_name,
        shape=shape,
        subnet_id=subnet_id,
        image_id=image_id,
        metadata={
            "ssh_authorized_keys": ssh_authorized_public_key,
        },
        shape_config=oci.core.models.LaunchInstanceShapeConfigDetails(
            memory_in_gbs=memory_in_gbs,
            ocpus=ocpus,
        ),
        create_vnic_details=vnic_details,
    )

    instance_response = compute.launch_instance(create_instance_details)
    return instance_response


async def workflow():
    if await exist_instance_shape(local_config["instance_shape"]):
        logging.warning(f"{local_config['instance_shape']} 인스턴스가 이미 존재합니다.")
        logging.warning("이제 이 프로세스를 종료해도 됩니다.")
        datetime_string = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        await log_hook.send(f"{datetime_string} | {local_config['instance_shape']} 인스턴스가 이미 존재합니다.")
    else:
        with open("./ssh_keys/public_key.pub", "r") as public_key_file:
            public_key = public_key_file.read()
        try:
            response = await create_compute_instance(
                compartment_id=local_config["compartment_id"],
                availability_domain=local_config["availability_domain"],
                display_name=local_config["instance_display_name"],
                shape=local_config["instance_shape"],
                subnet_id=local_config["subnet_id"],
                image_id=local_config["image_id"],
                memory_in_gbs=float(local_config["instance_memory_in_gbs"]),
                ocpus=float(local_config["instance_ocpus"]),
                ssh_authorized_public_key=public_key,
            )
            logging.warning(
                "%s 에 %s 인스턴스를 생성했습니다. (ID: %s)",
                response.data.availability_domain,
                response.data.display_name,
                response.data.id,
            )
            datetime_string = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            await log_hook.send(
                f"{datetime_string} | {response.data.availability_domain} 에 "
                f"{response.data.display_name} 인스턴스를 생성했습니다. (ID: {response.data.id})"
            )
        except oci.exceptions.ServiceError as err_data:
            if err_data.status == 500 and "Out of host capacity" in err_data.message:
                logging.warning(
                    "%s 인스턴스를 생성하지 못했습니다.", local_config["instance_display_name"]
                )
                logging.warning(
                    f"InternalError(500): {local_config['instance_shape']} 구성에 대한 용량이 부족합니다."
                )
                datetime_string = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                await log_hook.send(
                    f"{datetime_string} | {local_config['instance_display_name']} 인스턴스를 생성하지 못했습니다. "
                    f"(InternalError(500): {local_config['instance_shape']}, Out of host capacity)"
                )
            elif err_data.status == 429 or "TooManyRequests" in err_data.code:
                logging.warning("요청이 너무 많습니다. 1분 뒤에 다시 시작합니다. (429 TooManyRequests)")
                datetime_string = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                await log_hook.send(
                    f"{datetime_string} | 요청이 너무 많습니다. 1분 뒤에 다시 시작합니다. (429 TooManyRequests)"
                )
                await asyncio.sleep(60)
                logging.info("1분 대기가 끝났습니다. 다음 작업을 시작할 준비가 되었습니다. scheduler 로그를 확인하세요.")
            else:
                logging.warning("예기치 못한 오류가 발생했습니다.")
                logging.error(err_data)
                datetime_string = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

                error_fp = io.StringIO()
                error_fp.write(
                    json.dumps(
                        {
                            "status": err_data.status,
                            "code": err_data.code,
                            "opc-request-id": err_data.request_id,
                            "message": err_data.message,
                            "operation_name": err_data.operation_name,
                            "timestamp": err_data.timestamp,
                            "request_endpoint": err_data.request_endpoint,
                        },
                        indent=2,
                    )
                )
                error_fp.seek(0)
                await log_hook.send(
                    file=dhooks.File(fp=error_fp, name="error.json"),
                    content=f"{datetime_string} | 예기치 못한 오류가 발생했습니다.",
                )
