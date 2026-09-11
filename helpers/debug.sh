ss -tlnp | grep 5432
sudo less /var/log/cloud-init-output.log
cloud-init status --long
sudo tail -f /var/log/cloud-init-output.log
psql -h localhost -U dbuser mydb

ssh -L 8080:localhost:80 -J worms debian@10.3.0.17
ssh -L 8080:localhost:80 -J worms dbbrowser

ssh -D 1080 worms

cloud-init status --long


# !!!
sudo -u postgres psql mydb