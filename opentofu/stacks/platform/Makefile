INTERNET_ENABLED ?= true

apply:
	tofu apply -auto-approve -var internet_enabled=$(INTERNET_ENABLED)

destroy:
	tofu destroy -auto-approve -var internet_enabled=$(INTERNET_ENABLED)
