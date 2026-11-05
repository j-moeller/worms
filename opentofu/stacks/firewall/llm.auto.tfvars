# The one external address sandbox and devbox VMs may send to on TCP 443: the LLM
# endpoint, teachllm.mlsec.tu-berlin.de. Used by the llm-access security group
# (main.tf). Not a secret, so it is tracked, unlike other tfvars.
llm_access_ip = "141.23.117.34"
