from uuid import NAMESPACE_URL, uuid5

CONNECTION_ID = str(uuid5(NAMESPACE_URL, "contractbridge:examples:shopping:v2"))
OPERATION = "/v1/orders"
