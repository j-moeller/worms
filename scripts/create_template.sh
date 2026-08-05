# Create the VM (replace IDs/storage names as needed)
VMID=9000
TEMPLATE_NAME=ubuntu-template
STORAGE=local-zfs
IMAGE=images/resolute-server-cloudimg-amd64.img

qm create $VMID \
  --name $TEMPLATE_NAME \
  --memory 2048 \
  --cores 2 \
  --cpu host \
  --net0 virtio,bridge=vmbr0 \
  --agent enabled=1 \
  --serial0 socket \
  --vga serial0

# Import the Ubuntu cloud image
qm importdisk $VMID $IMAGE $STORAGE

# Attach imported disk (adjust "vm-9000-disk-0" if your storage names it differently)
qm set $VMID \
  --scsihw virtio-scsi-pci \
  --scsi0 ${STORAGE}:vm-${VMID}-disk-0

# Add Cloud-Init drive
qm set $VMID --ide2 ${STORAGE}:cloudinit

# Boot from the imported disk
qm set $VMID --boot order=scsi0

# Use VirtIO NIC
qm set $VMID --net0 virtio,bridge=vmbr0
qm set $VMID --ipconfig0 ip=dhcp

# Add SSH key
qm set $VMID --sshkey ~/.ssh/id_rsa.pub

qm set $VMID --ciuser root --cipassword password

# Convert to template
# qm template $VMID