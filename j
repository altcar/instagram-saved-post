
lsblk -o NAME,FSTYPE,UUID,MOUNTPOINTS,SERIAL,LABEL
wipefs -a /dev/sdb && parted /dev/sdb mklabel gpt && parted -a optimal /dev/sdb mkpart primary xfs 0% 100% && mkfs.xfs -f /dev/sdb1
sudo umount /dev/sdb*
sudo mdadm --stop /dev/sdb
sudo mdadm --zero-superblock /dev/sdb
sudo pvremove /dev/sdb --force --force -y
wipefs -a /dev/sdb 
fuser -v /dev/sdb
wipefs -a /dev/sdb 
sudo pvremove /dev/sdb1 /dev/sdb2 /dev/sdb3 --force --force -y
sudo dmsetup remove_all
wipefs -a /dev/sdb 
# Overwrite the first 100MB of the drive with zeros
sudo dd if=/dev/zero of=/dev/sdb bs=1M count=100 oflag=direct
sudo partprobe /dev/sdb
sudo swapon --show
sudo reboot
sudo parted /dev/sdb mklabel gpt && sudo parted -a optimal /dev/sdb mkpart primary xfs 0% 100% && sudo mkfs.xfs -f /dev/sdb1
wipefs -a /dev/sdb && parted /dev/sdb mklabel gpt && parted -a optimal /dev/sdb mkpart primary xfs 0% 100% && mkfs.xfs -f /dev/sdb1
lsblk -o NAME,FSTYPE,UUID,MOUNTPOINTS,SERIAL,LABEL
sudo parted /dev/sdc mklabel gpt && sudo parted -a optimal /dev/sdc mkpart primary xfs 0% 100% && sudo mkfs.xfs -f /dev/sdc1
# Overwrite the first 100MB of the drive with zeros
sudo dd if=/dev/zero of=/dev/sdc bs=1M count=100 oflag=direct
sudo dmsetup remove_all
sudo pvremove /dev/sdb1 /dev/sdb2 /dev/sdb3 --force --force -y
sudo pvremove /dev/sdc1 /dev/sdb2 /dev/sdb3 --force --force -y
sudo pvremove /dev/sdc1 /dev/sdb2 /dev/sdc3 --force --force -y
sudo pvremove /dev/sdc1 /dev/sdc2 /dev/sdc3 --force --force -y
lsblk -o NAME,FSTYPE,UUID,MOUNTPOINTS,SERIAL,LABEL
sudo dd if=/dev/zero of=/dev/sdc bs=1M count=100 oflag=directsudo parted /dev/sdc mklabel gpt && sudo parted -a optimal /dev/sdc mkpart primary xfs 0% 100% && sudo mkfs.xfs -f /dev/sdc1
sudo parted /dev/sdc mklabel gpt && sudo parted -a optimal /dev/sdc mkpart primary xfs 0% 100% && sudo mkfs.xfs -f /dev/sdc1
reboot
lsblk -o NAME,FSTYPE,UUID,MOUNTPOINTS,SERIAL,LABEL
apt update && apt upgrade && apt install samba nfs-kernel-server  mergerfs snapraid
sudo xfs_admin -L "4tb1" /dev/sdb1
sudo xfs_admin -L "4tb2" /dev/sdc1
mkdir -p /media/4tb1
mkdir -p /media/old
mkdir -p /media/4tb2
echo -e "$(lsblk -o NAME,FSTYPE,UUID,MOUNTPOINTS,SERIAL,LABEL | grep 'old8T-20232' | awk '{print "UUID="$3" /media/old "$2" defaults 0 2"}')" | sudo tee -a /etc/fstab
echo -e "$(lsblk -o NAME,FSTYPE,UUID,MOUNTPOINTS,SERIAL,LABEL | grep '4tb1' | awk '{print "UUID="$3" /media/4tb1 "$2" defaults 0 2"}')" | sudo tee -a /etc/fstab
echo -e "$(lsblk -o NAME,FSTYPE,UUID,MOUNTPOINTS,SERIAL,LABEL | grep '4tb2' | awk '{print "UUID="$3" /media/4tb2 "$2" defaults 0 2"}')" | sudo tee -a /etc/fstab
mkdir -p /media/storage
ls /media
echo -e "/media/*tb* /media/storage mergerfs defaults,allow_other,use_ino,category.create=mfs,minfreespace=20G,fsname=NAS_POOL 0 2" | sudo tee -a /etc/fstab
nano /etc/fstab
reboot
# Install tmux
sudo apt update && sudo apt install tmux -y
# Start a new session named 'move'
tmux new -s move
tmux attach -t move
hey
tmux attach -t move
ls /media/old
# Move from old to new with checksums
sudo rsync -avP --checksum /media/old/ /media/storage/old/
sudo rsync -avnc --delete /media/old/ /media/storage/old/
lsblk -o NAME,FSTYPE,UUID,MOUNTPOINTS,SERIAL,LABEL
sudo wipefs -a /dev/sdd
# Overwrite the first 100MB of the drive with zeros
sudo dd if=/dev/zero of=/dev/sdd bs=1M count=100 oflag=direct
sudo partprobe /dev/sdd
sudo wipefs -a /dev/sdd
reboot
tmux attach -t move
df -h
tmux attach -t move
mkdir -p /media/new8t
echo -e "$(lsblk -o NAME,FSTYPE,UUID,MOUNTPOINTS,SERIAL,LABEL | grep 'new8T-20238' | awk '{print "UUID="$3" /media/new8t "$2" defaults 0 2"}')" | sudo tee -a /etc/fstab
nano /etc/fstab
tmux attach -t move
ls -l "/media/old"
ls -l "/media/old8t"
ls -l /media
ls -l "/media/storage"
ls -l "/media/storage/old"
ls -l "/media/storage/old/up"
journalctl -xb
lsblk -o NAME,FSTYPE,UUID,MOUNTPOINTS,SERIAL,LABEL
ls /media
mkdir -p /media/8tb1
ls /media
sudo wipefs -a /dev/sdd
sudo parted /dev/sdd mklabel gpt
sudo parted -a optimal /dev/sdd mkpart primary xfs 0% 100%
sudo partprobe /dev/sdd
sudo mkfs.xfs -f /dev/sdd1
sudo xfs_admin -L "8tb1" /dev/sdd1
lsblk -o NAME,FSTYPE,UUID,MOUNTPOINTS,SERIAL,LABEL
nano /etc/fstab
ls /media
lsblk -o NAME,FSTYPE,UUID,MOUNTPOINTS,SERIAL,LABEL
reboot
shutdown -r now
exit
df -h
ls /media
ls /media/new8T
ls /media/new8t
ls /media/storage
sudo mkdir -p /media/storage/new
tmux new -s move_new8t
tmux new -s move
df -h
tmux new -s move
tmux attach -t move
hey
ls /media/new8t
tmux attach -t move
ls /media/new8t
ls /media/new8t/recording
ls -l /media/new8t/recording
ls -l /media/new8t/recording/迅雷下载
tmux attach -t move
sudo rsync -avP --checksum --modify-window=1 /media/new8t/ /media/storage/new/
df -h
ls /media/new8t
ls /media/storage/new
sudo rsync -avnc --delete --modify-window=1 /media/new8t/ /media/storage/new/
ls /media/new8t
ls /media/storage/new
df -h
reboot 
sudo tmux attach -t move
sudo mkdir -p /media/storage/3T
lsblk -o NAME,FSTYPE,UUID,MOUNTPOINTS,SERIAL,LABEL
sudo mkdir -p /media/storage/3ts
sudo mkdir -p /media/3ts
echo -e "$(lsblk -o NAME,FSTYPE,UUID,MOUNTPOINTS,SERIAL,LABEL | grep '3Tdrive' | awk '{print "UUID="$3" /media/3ts "$2" defaults 0 2"}')" | sudo tee -a /etc/fstab
reboot
ls /media/3t
ls /media/3ts
ls /media/storage
df -h
sudo mkdir -p /media/3ts
lsblk -o NAME,FSTYPE,UUID,MOUNTPOINTS,SERIAL,LABEL
ls /media/storage/3ts
ls /media/storage
sudo rsync -avP --checksum --modify-window=1 /media/3ts/ /media/storage/3ts/
ls /media/storage/3ts
bg
disown
ps aux | grep rsync
sudo tmux attach -t move
sudo tmux attach -s move
tmux new -s move
tmux attach -t move
# Check if the service is running
sudo systemctl status ssh
# Check if the port is actually open and listening
ss -tulpn | grep :22
nano /etc/ssh/sshd_config
sudo adduser yourname
grep '^sudo:.*' /etc/group
# This gives the user permission to this folder ONLY
sudo chown -R backupuser:backupuser /media/storage/old
sudo chown -R yourname:yourname /media/storage/old
ls /media/storage old
ls /media/storage/old
ls /media/storage/old/server
ls /media/storage/old/up
ls /media/storage/old/
ls /media/storage/old/server
ls /media/storage/old/docker
ls /media/storage/old
ls /media/storage/new
ps aux | grep rsync
ls /media/storage/old
ls /media/storage/old/server/docker
ls /media/storage/old/server/docker/backup
ls /media/storage/old/server/docker/backup/DS3622-1.hbk
sudo chown -R yourname:yourname /media/storage/old/server/docker/backup
sudo ufw allow ssh
sudo ufw status
sudo ufw enable
sudo systemctl restart ssh
tmux attach -t move
ss -tulpn | grep :5522
sudo ufw status
sudo ufw allow 5522/tcp
sudo ufw delete allow 22/tcp
sudo ufw status
# Give everyone 'search' permission on the mount points 
# (This doesn't let them see files, just "pass through" to the next folder)
sudo chmod +x /media
sudo chmod +x /media/storage
sudo chmod +x /media/storage/old
sudo chmod +x /media/storage/old/server
sudo chmod +x /media/storage/old/server/docker
# Finally, give your user full ownership of the target
sudo chown -R yourname:yourname /media/storage/old/server/docker/backup
# Set full permissions for your user on the target backup folder
sudo chmod 755 /media/storage/old/server/docker/backup
sudo chown yourname:yourname /media/storage/old/server/docker/backup
cat /etc/rsyncd.conf
sudo nano /etc/ssh/sshd_config
# Allow the new port
sudo ufw allow 5223/tcp
# Remove the old port rule
sudo ufw delete allow 5522/tcp
# Restart the SSH service to apply the change
sudo systemctl restart ssh
ssh-keygen -R [192.168.0.201]:5522
ssh-keygen -R [192.168.0.201]:5223
ps aux | grep rsync
# This updates every 5 seconds so you can watch the "Used" space climb
watch -n 5 du -sh /media/storage/3ts
df -h
while ps aux | grep -v grep | grep -q rsync; do sleep 10; done && echo "TRANSFER FINISHED"
while ps aux | grep -v grep | grep -q rsync; do    echo "Rsync is still moving data... $(date +%H:%M:%S)";   sleep 60; done
echo "DONE! The transfer has finished."
reboot
bg
df -h
sudo tmux attach -t move
df -h
sudo tmux attach -t move
df -h
# Wipe partition table
sudo wipefs -a /dev/sde
# Create XFS filesystem (Label it parity1)
sudo mkfs.xfs -f -L parity1 /dev/sde
sudo dd if=/dev/zero of=/dev/sde bs=1M count=100 oflag=direct # Overwrite the first 100MB of the dri
df -h
reboot
lsblk -o NAME,FSTYPE,UUID,MOUNTPOINTS,SERIAL,LABEL
sudo su
sudo mkfs.xfs -f -L parity1 /dev/sde
lsblk -o NAME,FSTYPE,UUID,MOUNTPOINTS,SERIAL,LABEL
sudo parted /dev/sde mklabel gpt && sudo parted -a optimal /dev/sde mkpart primary xfs 0% 100% && sudo mkfs.xfs -f /dev/sde1
lsblk -o NAME,FSTYPE,UUID,MOUNTPOINTS,SERIAL,LABEL
sudo xfs_admin -L "parity1-8T20238" /dev/sde1
lsblk -o NAME,FSTYPE,UUID,MOUNTPOINTS,SERIAL,LABEL
sudo nano /etc/fstab
/media/parity1
mkdir -p /media/parity1
echo -e "$(lsblk -o NAME,FSTYPE,UUID,MOUNTPOINTS,SERIAL,LABEL | grep 'parity' | awk '{print "UUID="$3" /media/parity1 "$2" defaults 0 2"}')" | sudo tee -a /etc/fstab
sudo nano /etc/fstab
lsblk -o NAME,FSTYPE,UUID,MOUNTPOINTS,SERIAL,LABEL
sudo umount /dev/sdd1
sudo xfs_admin -L "old8T-20232" /dev/sdd1
sudo mount /dev/sdb1
sudo mount /dev/sdd1
df -h
lsblk -o NAME,FSTYPE,UUID,MOUNTPOINTS,SERIAL,LABEL
sudo xfs_admin -L "new8T-20238" /dev/sde1
snapraid
sudo nano /etc/snapraid.conf
sudo mkdir -p /var/snapraid
timedatectl
reboot
tmux new -s snapraid
sudo nano /usr/local/bin/snapraid-runner.sh
sudo nano /usr/local/bin/discord-msg.sh