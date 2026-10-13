INTERNET_ENABLED ?= true

apply:
	tofu apply -auto-approve -parallelism=1 -var internet_enabled=$(INTERNET_ENABLED)

destroy:
	tofu destroy -auto-approve -var internet_enabled=$(INTERNET_ENABLED)
