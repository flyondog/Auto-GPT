import logging
import uuid
from collections import defaultdict
from typing import TYPE_CHECKING, Any, Literal, Optional, cast

from prisma.enums import SubmissionStatus
from prisma.models import AgentGraph, AgentNode, AgentNodeLink, StoreListingVersion
from prisma.types import (
    AgentGraphCreateInput,
    AgentGraphWhereInput,
    AgentNodeCreateInput,
    AgentNodeLinkCreateInput,
    StoreListingVersionWhereInput,
)
from pydantic import Field, JsonValue, create_model
from pydantic.fields import computed_field

from backend.blocks.agent import AgentExecutorBlock
from backend.blocks.io import AgentInputBlock, AgentOutputBlock
from backend.blocks.llm import LlmModel
from backend.data.db import prisma as db
from backend.data.model import (
    CredentialsField,
    CredentialsFieldInfo,
    CredentialsMetaInput,
    is_credentials_field_name,
)
from backend.integrations.providers import ProviderName
from backend.util import type as type_utils
from backend.util.json import SafeJson

from .block import Block, BlockInput, BlockSchema, BlockType, get_block, get_blocks
from .db import BaseDbModel, query_raw_with_schema, transaction
from .includes import AGENT_GRAPH_INCLUDE, AGENT_NODE_INCLUDE

if TYPE_CHECKING:
    from .integrations import Webhook

logger = logging.getLogger(__name__)


class Link(BaseDbModel):
    source_id: str
    sink_id: str
    source_name: str
    sink_name: str
    is_static: bool = False

    @staticmethod
    def from_db(link: AgentNodeLink):
        return Link(
            id=link.id,
            source_name=link.sourceName,
            source_id=link.agentNodeSourceId,
            sink_name=link.sinkName,
            sink_id=link.agentNodeSinkId,
            is_static=link.isStatic,
        )

    def __hash__(self):
        return hash((self.source_id, self.sink_id, self.source_name, self.sink_name))


class Node(BaseDbModel):
    block_id: str
    input_default: BlockInput = {}  # dict[input_name, default_value]
    metadata: dict[str, Any] = {}
    input_links: list[Link] = []
    output_links: list[Link] = []

    @property
    def block(self) -> Block[BlockSchema, BlockSchema]:
        block = get_block(self.block_id)
        if not block:
            raise ValueError(
                f"Block #{self.block_id} does not exist -> Node #{self.id} is invalid"
            )
        return block


class NodeModel(Node):
    graph_id: str
    graph_version: int

    webhook_id: Optional[str] = None
    webhook: Optional["Webhook"] = None

    @staticmethod
    def from_db(node: AgentNode, for_export: bool = False) -> "NodeModel":
        from .integrations import Webhook

        obj = NodeModel(
            id=node.id,
            block_id=node.agentBlockId,
            input_default=type_utils.convert(node.constantInput, dict[str, Any]),
            metadata=type_utils.convert(node.metadata, dict[str, Any]),
            graph_id=node.agentGraphId,
            graph_version=node.agentGraphVersion,
            webhook_id=node.webhookId,
            webhook=Webhook.from_db(node.Webhook) if node.Webhook else None,
        )
        obj.input_links = [Link.from_db(link) for link in node.Input or []]
        obj.output_links = [Link.from_db(link) for link in node.Output or []]
        if for_export:
            return obj.stripped_for_export()
        return obj

    def is_triggered_by_event_type(self, event_type: str) -> bool:
        return self.block.is_triggered_by_event_type(self.input_default, event_type)

    def stripped_for_export(self) -> "NodeModel":
        """
        Returns a copy of the node model, stripped of any non-transferable properties
        """
        stripped_node = self.model_copy(deep=True)
        # Remove credentials from node input
        if stripped_node.input_default:
            stripped_node.input_default = NodeModel._filter_secrets_from_node_input(
                stripped_node.input_default, self.block.input_schema.jsonschema()
            )

        if (
            stripped_node.block.block_type == BlockType.INPUT
            and "value" in stripped_node.input_default
        ):
            stripped_node.input_default["value"] = ""

        # Remove webhook info
        stripped_node.webhook_id = None
        stripped_node.webhook = None

        return stripped_node

    @staticmethod
    def _filter_secrets_from_node_input(
        input_data: dict[str, Any], schema: dict[str, Any] | None
    ) -> dict[str, Any]:
        sensitive_keys = ["credentials", "api_key", "password", "token", "secret"]
        field_schemas = schema.get("properties", {}) if schema else {}
        result = {}
        for key, value in input_data.items():
            field_schema: dict | None = field_schemas.get(key)
            if (field_schema and field_schema.get("secret", False)) or any(
                sensitive_key in key.lower() for sensitive_key in sensitive_keys
            ):
                # This is a secret value -> filter this key-value pair out
                continue
            elif isinstance(value, dict):
                result[key] = NodeModel._filter_secrets_from_node_input(
                    value, field_schema
                )
            else:
                result[key] = value
        return result


class BaseGraph(BaseDbModel):
    version: int = 1
    is_active: bool = True
    name: str
    description: str
    nodes: list[Node] = []
    links: list[Link] = []
    forked_from_id: str | None = None
    forked_from_version: int | None = None

    @computed_field
    @property
    def input_schema(self) -> dict[str, Any]:
        return self._generate_schema(
            *(
                (block.input_schema, node.input_default)
                for node in self.nodes
                if (block := node.block).block_type == BlockType.INPUT
                and issubclass(block.input_schema, AgentInputBlock.Input)
            )
        )

    @computed_field
    @property
    def output_schema(self) -> dict[str, Any]:
        return self._generate_schema(
            *(
                (block.input_schema, node.input_default)
                for node in self.nodes
                if (block := node.block).block_type == BlockType.OUTPUT
                and issubclass(block.input_schema, AgentOutputBlock.Input)
            )
        )

    @computed_field
    @property
    def has_external_trigger(self) -> bool:
        return self.webhook_input_node is not None

    @property
    def webhook_input_node(self) -> Node | None:
        return next(
            (
                node
                for node in self.nodes
                if node.block.block_type
                in (BlockType.WEBHOOK, BlockType.WEBHOOK_MANUAL)
            ),
            None,
        )

    @staticmethod
    def _generate_schema(
        *props: tuple[type[AgentInputBlock.Input] | type[AgentOutputBlock.Input], dict],
    ) -> dict[str, Any]:
        schema_fields: list[AgentInputBlock.Input | AgentOutputBlock.Input] = []
        for type_class, input_default in props:
            try:
                schema_fields.append(type_class.model_construct(**input_default))
            except Exception as e:
                logger.error(f"Invalid {type_class}: {input_default}, {e}")

        return {
            "type": "object",
            "properties": {
                p.name: {
                    **{
                        k: v
                        for k, v in p.generate_schema().items()
                        if k not in ["description", "default"]
                    },
                    "secret": p.secret,
                    # Default value has to be set for advanced fields.
                    "advanced": p.advanced and p.value is not None,
                    "title": p.title or p.name,
                    **({"description": p.description} if p.description else {}),
                    **({"default": p.value} if p.value is not None else {}),
                }
                for p in schema_fields
            },
            "required": [p.name for p in schema_fields if p.value is None],
        }


class Graph(BaseGraph):
    sub_graphs: list[BaseGraph] = []  # Flattened sub-graphs

    @computed_field
    @property
    def credentials_input_schema(self) -> dict[str, Any]:
        return self._credentials_input_schema.jsonschema()

    @property
    def _credentials_input_schema(self) -> type[BlockSchema]:
        graph_credentials_inputs = self.aggregate_credentials_inputs()
        logger.debug(
            f"Combined credentials input fields for graph #{self.id} ({self.name}): "
            f"{graph_credentials_inputs}"
        )

        # Warn if same-provider credentials inputs can't be combined (= bad UX)
        graph_cred_fields = list(graph_credentials_inputs.values())
        for i, (field, keys) in enumerate(graph_cred_fields):
            for other_field, other_keys in list(graph_cred_fields)[i + 1 :]:
                if field.provider != other_field.provider:
                    continue
                if ProviderName.HTTP in field.provider:
                    continue

                # If this happens, that means a block implementation probably needs
                # to be updated.
                logger.warning(
                    "Multiple combined credentials fields "
                    f"for provider {field.provider} "
                    f"on graph #{self.id} ({self.name}); "
                    f"fields: {field} <> {other_field};"
                    f"keys: {keys} <> {other_keys}."
                )

        fields: dict[str, tuple[type[CredentialsMetaInput], CredentialsMetaInput]] = {
            agg_field_key: (
                CredentialsMetaInput[
                    Literal[tuple(field_info.provider)],  # type: ignore
                    Literal[tuple(field_info.supported_types)],  # type: ignore
                ],
                CredentialsField(
                    required_scopes=set(field_info.required_scopes or []),
                    discriminator=field_info.discriminator,
                    discriminator_mapping=field_info.discriminator_mapping,
                    discriminator_values=field_info.discriminator_values,
                ),
            )
            for agg_field_key, (field_info, _) in graph_credentials_inputs.items()
        }

        return create_model(
            self.name.replace(" ", "") + "CredentialsInputSchema",
            __base__=BlockSchema,
            **fields,  # type: ignore
        )

    def aggregate_credentials_inputs(
        self,
    ) -> dict[str, tuple[CredentialsFieldInfo, set[tuple[str, str]]]]:
        """
        Returns:
            dict[aggregated_field_key, tuple(
                CredentialsFieldInfo: A spec for one aggregated credentials field
                    (now includes discriminator_values from matching nodes)
                set[(node_id, field_name)]: Node credentials fields that are
                    compatible with this aggregated field spec
            )]
        """
        # First collect all credential field data with input defaults
        node_credential_data = []

        for graph in [self] + self.sub_graphs:
            for node in graph.nodes:
                for (
                    field_name,
                    field_info,
                ) in node.block.input_schema.get_credentials_fields_info().items():

                    discriminator = field_info.discriminator
                    if not discriminator:
                        node_credential_data.append((field_info, (node.id, field_name)))
                        continue

                    discriminator_value = node.input_default.get(discriminator)
                    if discriminator_value is None:
                        node_credential_data.append((field_info, (node.id, field_name)))
                        continue

                    discriminated_info = field_info.discriminate(discriminator_value)
                    discriminated_info.discriminator_values.add(discriminator_value)

                    node_credential_data.append(
                        (discriminated_info, (node.id, field_name))
                    )

        # Combine credential field info (this will merge discriminator_values automatically)
        return CredentialsFieldInfo.combine(*node_credential_data)


class GraphModel(Graph):
    user_id: str
    nodes: list[NodeModel] = []  # type: ignore

    @property
    def starting_nodes(self) -> list[NodeModel]:
        outbound_nodes = {link.sink_id for link in self.links}
        input_nodes = {
            node.id for node in self.nodes if node.block.block_type == BlockType.INPUT
        }
        return [
            node
            for node in self.nodes
            if node.id not in outbound_nodes or node.id in input_nodes
        ]

    def meta(self) -> "GraphMeta":
        """
        Returns a GraphMeta object with metadata about the graph.
        This is used to return metadata about the graph without exposing nodes and links.
        """
        return GraphMeta.from_graph(self)

    def reassign_ids(self, user_id: str, reassign_graph_id: bool = False):
        """
        Reassigns all IDs in the graph to new UUIDs.
        This method can be used before storing a new graph to the database.
        """
        if reassign_graph_id:
            graph_id_map = {
                self.id: str(uuid.uuid4()),
                **{sub_graph.id: str(uuid.uuid4()) for sub_graph in self.sub_graphs},
            }
        else:
            graph_id_map = {}

        self._reassign_ids(self, user_id, graph_id_map)
        for sub_graph in self.sub_graphs:
            self._reassign_ids(sub_graph, user_id, graph_id_map)

    @staticmethod
    def _reassign_ids(
        graph: BaseGraph,
        user_id: str,
        graph_id_map: dict[str, str],
    ):
        # Reassign Graph ID
        if graph.id in graph_id_map:
            graph.id = graph_id_map[graph.id]

        # Reassign Node IDs
        id_map = {node.id: str(uuid.uuid4()) for node in graph.nodes}
        for node in graph.nodes:
            node.id = id_map[node.id]

        # Reassign Link IDs
        for link in graph.links:
            if link.source_id in id_map:
                link.source_id = id_map[link.source_id]
            if link.sink_id in id_map:
                link.sink_id = id_map[link.sink_id]

        # Reassign User IDs for agent blocks
        for node in graph.nodes:
            if node.block_id != AgentExecutorBlock().id:
                continue
            node.input_default["user_id"] = user_id
            node.input_default.setdefault("inputs", {})
            if (
                graph_id := node.input_default.get("graph_id")
            ) and graph_id in graph_id_map:
                node.input_default["graph_id"] = graph_id_map[graph_id]

    def validate_graph(
        self,
        for_run: bool = False,
        nodes_input_masks: Optional[dict[str, dict[str, JsonValue]]] = None,
    ):
        self._validate_graph(self, for_run, nodes_input_masks)
        for sub_graph in self.sub_graphs:
            self._validate_graph(sub_graph, for_run, nodes_input_masks)

    @staticmethod
    def _validate_graph(
        graph: BaseGraph,
        for_run: bool = False,
        nodes_input_masks: Optional[dict[str, dict[str, JsonValue]]] = None,
    ):
        def is_tool_pin(name: str) -> bool:
            return name.startswith("tools_^_")

        def sanitize(name):
            sanitized_name = name.split("_#_")[0].split("_@_")[0].split("_$_")[0]
            if is_tool_pin(sanitized_name):
                return "tools"
            return sanitized_name

        # Validate smart decision maker nodes
        nodes_block = {
            node.id: block
            for node in graph.nodes
            if (block := get_block(node.block_id)) is not None
        }

        input_links = defaultdict(list)

        for link in graph.links:
            input_links[link.sink_id].append(link)

        # Nodes: required fields are filled or connected and dependencies are satisfied
        for node in graph.nodes:
            if (block := nodes_block.get(node.id)) is None:
                raise ValueError(f"Invalid block {node.block_id} for node #{node.id}")

            node_input_mask = (
                nodes_input_masks.get(node.id, {}) if nodes_input_masks else {}
            )
            provided_inputs = set(
                [sanitize(name) for name in node.input_default]
                + [sanitize(link.sink_name) for link in input_links.get(node.id, [])]
                + ([name for name in node_input_mask] if node_input_mask else [])
            )
            InputSchema = block.input_schema
            for name in (required_fields := InputSchema.get_required_fields()):
                if (
                    name not in provided_inputs
                    # Checking availability of credentials is done by ExecutionManager
                    and name not in InputSchema.get_credentials_fields()
                    # Validate only I/O nodes, or validate everything when executing
                    and (
                        for_run
                        or block.block_type
                        in [
                            BlockType.INPUT,
                            BlockType.OUTPUT,
                            BlockType.AGENT,
                        ]
                    )
                ):
                    raise ValueError(
                        f"Node {block.name} #{node.id} required input missing: `{name}`"
                    )

                if (
                    block.block_type == BlockType.INPUT
                    and (input_key := node.input_default.get("name"))
                    and is_credentials_field_name(input_key)
                ):
                    raise ValueError(
                        f"Agent input node uses reserved name '{input_key}'; "
                        "'credentials' and `*_credentials` are reserved input names"
                    )

            # Get input schema properties and check dependencies
            input_fields = InputSchema.model_fields

            def has_value(node: Node, name: str):
                return (
                    (
                        name in node.input_default
                        and node.input_default[name] is not None
                        and str(node.input_default[name]).strip() != ""
                    )
                    or (name in input_fields and input_fields[name].default is not None)
                    or (
                        name in node_input_mask
                        and node_input_mask[name] is not None
                        and str(node_input_mask[name]).strip() != ""
                    )
                )

            # Validate dependencies between fields
            for field_name in input_fields.keys():
                field_json_schema = InputSchema.get_field_schema(field_name)

                dependencies: list[str] = []

                # Check regular field dependencies (only pre graph execution)
                if for_run:
                    dependencies.extend(field_json_schema.get("depends_on", []))

                # Require presence of credentials discriminator (always).
                # The `discriminator` is either the name of a sibling field (str),
                # or an object that discriminates between possible types for this field:
                # {"propertyName": prop_name, "mapping": {prop_value: sub_schema}}
                if (
                    discriminator := field_json_schema.get("discriminator")
                ) and isinstance(discriminator, str):
                    dependencies.append(discriminator)

                if not dependencies:
                    continue

                # Check if dependent field has value in input_default
                field_has_value = has_value(node, field_name)
                field_is_required = field_name in required_fields

                # Check for missing dependencies when dependent field is present
                missing_deps = [dep for dep in dependencies if not has_value(node, dep)]
                if missing_deps and (field_has_value or field_is_required):
                    raise ValueError(
                        f"Node {block.name} #{node.id}: Field `{field_name}` requires [{', '.join(missing_deps)}] to be set"
                    )

        node_map = {v.id: v for v in graph.nodes}

        def is_static_output_block(nid: str) -> bool:
            return node_map[nid].block.static_output

        # Links: links are connected and the connected pin data type are compatible.
        for link in graph.links:
            source = (link.source_id, link.source_name)
            sink = (link.sink_id, link.sink_name)
            prefix = f"Link {source} <-> {sink}"

            for i, (node_id, name) in enumerate([source, sink]):
                node = node_map.get(node_id)
                if not node:
                    raise ValueError(
                        f"{prefix}, {node_id} is invalid node id, available nodes: {node_map.keys()}"
                    )

                block = get_block(node.block_id)
                if not block:
                    blocks = {v().id: v().name for v in get_blocks().values()}
                    raise ValueError(
                        f"{prefix}, {node.block_id} is invalid block id, available blocks: {blocks}"
                    )

                sanitized_name = sanitize(name)
                vals = node.input_default
                if i == 0:
                    fields = (
                        block.output_schema.get_fields()
                        if block.block_type not in [BlockType.AGENT]
                        else vals.get("output_schema", {}).get("properties", {}).keys()
                    )
                else:
                    fields = (
                        block.input_schema.get_fields()
                        if block.block_type not in [BlockType.AGENT]
                        else vals.get("input_schema", {}).get("properties", {}).keys()
                    )
                if sanitized_name not in fields and not is_tool_pin(name):
                    fields_msg = f"Allowed fields: {fields}"
                    raise ValueError(f"{prefix}, `{name}` invalid, {fields_msg}")

            if is_static_output_block(link.source_id):
                link.is_static = True  # Each value block output should be static.

    @staticmethod
    def from_db(
        graph: AgentGraph,
        for_export: bool = False,
        sub_graphs: list[AgentGraph] | None = None,
    ) -> "GraphModel":
        return GraphModel(
            id=graph.id,
            user_id=graph.userId if not for_export else "",
            version=graph.version,
            forked_from_id=graph.forkedFromId,
            forked_from_version=graph.forkedFromVersion,
            is_active=graph.isActive,
            name=graph.name or "",
            description=graph.description or "",
            nodes=[NodeModel.from_db(node, for_export) for node in graph.Nodes or []],
            links=list(
                {
                    Link.from_db(link)
                    for node in graph.Nodes or []
                    for link in (node.Input or []) + (node.Output or [])
                }
            ),
            sub_graphs=[
                GraphModel.from_db(sub_graph, for_export)
                for sub_graph in sub_graphs or []
            ],
        )


class GraphMeta(Graph):
    user_id: str

    # Easy work-around to prevent exposing nodes and links in the API response
    nodes: list[NodeModel] = Field(default=[], exclude=True)  # type: ignore
    links: list[Link] = Field(default=[], exclude=True)

    @staticmethod
    def from_graph(graph: GraphModel) -> "GraphMeta":
        return GraphMeta(**graph.model_dump())


# --------------------- CRUD functions --------------------- #


async def get_node(node_id: str) -> NodeModel:
    """⚠️ No `user_id` check: DO NOT USE without check in user-facing endpoints."""
    node = await AgentNode.prisma().find_unique_or_raise(
        where={"id": node_id},
        include=AGENT_NODE_INCLUDE,
    )
    return NodeModel.from_db(node)


async def set_node_webhook(node_id: str, webhook_id: str | None) -> NodeModel:
    """⚠️ No `user_id` check: DO NOT USE without check in user-facing endpoints."""
    node = await AgentNode.prisma().update(
        where={"id": node_id},
        data=(
            {"Webhook": {"connect": {"id": webhook_id}}}
            if webhook_id
            else {"Webhook": {"disconnect": True}}
        ),
        include=AGENT_NODE_INCLUDE,
    )
    if not node:
        raise ValueError(f"Node #{node_id} not found")
    return NodeModel.from_db(node)


async def list_graphs(
    user_id: str,
    filter_by: Literal["active"] | None = "active",
) -> list[GraphMeta]:
    """
    Retrieves graph metadata objects.
    Default behaviour is to get all currently active graphs.

    Args:
        filter_by: An optional filter to either select graphs.
        user_id: The ID of the user that owns the graph.

    Returns:
        list[GraphMeta]: A list of objects representing the retrieved graphs.
    """
    where_clause: AgentGraphWhereInput = {"userId": user_id}

    if filter_by == "active":
        where_clause["isActive"] = True

    graphs = await AgentGraph.prisma().find_many(
        where=where_clause,
        distinct=["id"],
        order={"version": "desc"},
        include=AGENT_GRAPH_INCLUDE,
    )

    graph_models: list[GraphMeta] = []
    for graph in graphs:
        try:
            graph_meta = GraphModel.from_db(graph).meta()
            # Trigger serialization to validate that the graph is well formed
            graph_meta.model_dump()
            graph_models.append(graph_meta)
        except Exception as e:
            logger.error(f"Error processing graph {graph.id}: {e}")
            continue

    return graph_models


async def get_graph_metadata(graph_id: str, version: int | None = None) -> Graph | None:
    where_clause: AgentGraphWhereInput = {
        "id": graph_id,
    }

    if version is not None:
        where_clause["version"] = version

    graph = await AgentGraph.prisma().find_first(
        where=where_clause,
        order={"version": "desc"},
    )

    if not graph:
        return None

    return Graph(
        id=graph.id,
        name=graph.name or "",
        description=graph.description or "",
        version=graph.version,
        is_active=graph.isActive,
    )


async def get_graph(
    graph_id: str,
    version: int | None = None,
    user_id: str | None = None,
    for_export: bool = False,
    include_subgraphs: bool = False,
) -> GraphModel | None:
    """
    Retrieves a graph from the DB.
    Defaults to the version with `is_active` if `version` is not passed.

    Returns `None` if the record is not found.
    """
    where_clause: AgentGraphWhereInput = {
        "id": graph_id,
    }

    if version is not None:
        where_clause["version"] = version

    graph = await AgentGraph.prisma().find_first(
        where=where_clause,
        include=AGENT_GRAPH_INCLUDE,
        order={"version": "desc"},
    )
    if graph is None:
        return None

    if graph.userId != user_id:
        store_listing_filter: StoreListingVersionWhereInput = {
            "agentGraphId": graph_id,
            "isDeleted": False,
            "submissionStatus": SubmissionStatus.APPROVED,
        }
        if version is not None:
            store_listing_filter["agentGraphVersion"] = version

        # For access, the graph must be owned by the user or listed in the store
        if not await StoreListingVersion.prisma().find_first(
            where=store_listing_filter, order={"agentGraphVersion": "desc"}
        ):
            return None

    if include_subgraphs or for_export:
        sub_graphs = await get_sub_graphs(graph)
        return GraphModel.from_db(
            graph=graph,
            sub_graphs=sub_graphs,
            for_export=for_export,
        )

    return GraphModel.from_db(graph, for_export)


async def get_graph_as_admin(
    graph_id: str,
    version: int | None = None,
    user_id: str | None = None,
    for_export: bool = False,
) -> GraphModel | None:
    """
    Intentionally parallels the get_graph but should only be used for admin tasks, because can return any graph that's been submitted
    Retrieves a graph from the DB.
    Defaults to the version with `is_active` if `version` is not passed.

    Returns `None` if the record is not found.
    """
    logger.warning(f"Getting {graph_id=} {version=} as ADMIN {user_id=} {for_export=}")
    where_clause: AgentGraphWhereInput = {
        "id": graph_id,
    }

    if version is not None:
        where_clause["version"] = version

    graph = await AgentGraph.prisma().find_first(
        where=where_clause,
        include=AGENT_GRAPH_INCLUDE,
        order={"version": "desc"},
    )

    # For access, the graph must be owned by the user or listed in the store
    if graph is None or (
        graph.userId != user_id
        and not (
            await StoreListingVersion.prisma().find_first(
                where={
                    "agentGraphId": graph_id,
                    "agentGraphVersion": version or graph.version,
                }
            )
        )
    ):
        return None

    if for_export:
        sub_graphs = await get_sub_graphs(graph)
        return GraphModel.from_db(
            graph=graph,
            sub_graphs=sub_graphs,
            for_export=for_export,
        )

    return GraphModel.from_db(graph, for_export)


async def get_sub_graphs(graph: AgentGraph) -> list[AgentGraph]:
    """
    Iteratively fetches all sub-graphs of a given graph, and flattens them into a list.
    This call involves a DB fetch in batch, breadth-first, per-level of graph depth.
    On each DB fetch we will only fetch the sub-graphs that are not already in the list.
    """
    sub_graphs = {graph.id: graph}
    search_graphs = [graph]
    agent_block_id = AgentExecutorBlock().id

    while search_graphs:
        sub_graph_ids = [
            (graph_id, graph_version)
            for graph in search_graphs
            for node in graph.Nodes or []
            if (
                node.AgentBlock
                and node.AgentBlock.id == agent_block_id
                and (graph_id := cast(str, dict(node.constantInput).get("graph_id")))
                and (
                    graph_version := cast(
                        int, dict(node.constantInput).get("graph_version")
                    )
                )
            )
        ]
        if not sub_graph_ids:
            break

        graphs = await AgentGraph.prisma().find_many(
            where={
                "OR": [
                    {
                        "id": graph_id,
                        "version": graph_version,
                        "userId": graph.userId,  # Ensure the sub-graph is owned by the same user
                    }
                    for graph_id, graph_version in sub_graph_ids
                ]
            },
            include=AGENT_GRAPH_INCLUDE,
        )

        search_graphs = [graph for graph in graphs if graph.id not in sub_graphs]
        sub_graphs.update({graph.id: graph for graph in search_graphs})

    return [g for g in sub_graphs.values() if g.id != graph.id]


async def get_connected_output_nodes(node_id: str) -> list[tuple[Link, Node]]:
    links = await AgentNodeLink.prisma().find_many(
        where={"agentNodeSourceId": node_id},
        include={"AgentNodeSink": {"include": AGENT_NODE_INCLUDE}},
    )
    return [
        (Link.from_db(link), NodeModel.from_db(link.AgentNodeSink))
        for link in links
        if link.AgentNodeSink
    ]


async def set_graph_active_version(graph_id: str, version: int, user_id: str) -> None:
    # Activate the requested version if it exists and is owned by the user.
    updated_count = await AgentGraph.prisma().update_many(
        data={"isActive": True},
        where={
            "id": graph_id,
            "version": version,
            "userId": user_id,
        },
    )
    if updated_count == 0:
        raise Exception(f"Graph #{graph_id} v{version} not found or not owned by user")

    # Deactivate all other versions.
    await AgentGraph.prisma().update_many(
        data={"isActive": False},
        where={
            "id": graph_id,
            "version": {"not": version},
            "userId": user_id,
            "isActive": True,
        },
    )


async def get_graph_all_versions(graph_id: str, user_id: str) -> list[GraphModel]:
    graph_versions = await AgentGraph.prisma().find_many(
        where={"id": graph_id, "userId": user_id},
        order={"version": "desc"},
        include=AGENT_GRAPH_INCLUDE,
    )

    if not graph_versions:
        return []

    return [GraphModel.from_db(graph) for graph in graph_versions]


async def delete_graph(graph_id: str, user_id: str) -> int:
    entries_count = await AgentGraph.prisma().delete_many(
        where={"id": graph_id, "userId": user_id}
    )
    if entries_count:
        logger.info(f"Deleted {entries_count} graph entries for Graph #{graph_id}")
    return entries_count


async def create_graph(graph: Graph, user_id: str) -> GraphModel:
    async with transaction() as tx:
        await __create_graph(tx, graph, user_id)

    if created_graph := await get_graph(graph.id, graph.version, user_id=user_id):
        return created_graph

    raise ValueError(f"Created graph {graph.id} v{graph.version} is not in DB")


async def fork_graph(graph_id: str, graph_version: int, user_id: str) -> GraphModel:
    """
    Forks a graph by copying it and all its nodes and links to a new graph.
    """
    graph = await get_graph(graph_id, graph_version, user_id, True)
    if not graph:
        raise ValueError(f"Graph {graph_id} v{graph_version} not found")

    # Set forked from ID and version as itself as it's about ot be copied
    graph.forked_from_id = graph.id
    graph.forked_from_version = graph.version
    graph.name = f"{graph.name} (copy)"
    graph.reassign_ids(user_id=user_id, reassign_graph_id=True)
    graph.validate_graph(for_run=False)

    async with transaction() as tx:
        await __create_graph(tx, graph, user_id)

    return graph


async def __create_graph(tx, graph: Graph, user_id: str):
    graphs = [graph] + graph.sub_graphs

    await AgentGraph.prisma(tx).create_many(
        data=[
            AgentGraphCreateInput(
                id=graph.id,
                version=graph.version,
                name=graph.name,
                description=graph.description,
                isActive=graph.is_active,
                userId=user_id,
                forkedFromId=graph.forked_from_id,
                forkedFromVersion=graph.forked_from_version,
            )
            for graph in graphs
        ]
    )

    await AgentNode.prisma(tx).create_many(
        data=[
            AgentNodeCreateInput(
                id=node.id,
                agentGraphId=graph.id,
                agentGraphVersion=graph.version,
                agentBlockId=node.block_id,
                constantInput=SafeJson(node.input_default),
                metadata=SafeJson(node.metadata),
            )
            for graph in graphs
            for node in graph.nodes
        ]
    )

    await AgentNodeLink.prisma(tx).create_many(
        data=[
            AgentNodeLinkCreateInput(
                id=str(uuid.uuid4()),
                sourceName=link.source_name,
                sinkName=link.sink_name,
                agentNodeSourceId=link.source_id,
                agentNodeSinkId=link.sink_id,
                isStatic=link.is_static,
            )
            for graph in graphs
            for link in graph.links
        ]
    )


# ------------------------ UTILITIES ------------------------ #


def make_graph_model(creatable_graph: Graph, user_id: str) -> GraphModel:
    """
    Convert a Graph to a GraphModel, setting graph_id and graph_version on all nodes.

    Args:
        creatable_graph (Graph): The creatable graph to convert.
        user_id (str): The ID of the user creating the graph.

    Returns:
        GraphModel: The converted Graph object.
    """
    # Create a new Graph object, inheriting properties from CreatableGraph
    return GraphModel(
        **creatable_graph.model_dump(exclude={"nodes"}),
        user_id=user_id,
        nodes=[
            NodeModel(
                **creatable_node.model_dump(),
                graph_id=creatable_graph.id,
                graph_version=creatable_graph.version,
            )
            for creatable_node in creatable_graph.nodes
        ],
    )


async def fix_llm_provider_credentials():
    """Fix node credentials with provider `llm`"""
    from backend.integrations.credentials_store import IntegrationCredentialsStore

    from .user import get_user_integrations

    store = IntegrationCredentialsStore()

    broken_nodes = []
    try:
        broken_nodes = await query_raw_with_schema(
            """
            SELECT    graph."userId"       user_id,
                  node.id              node_id,
                  node."constantInput" node_preset_input
            FROM      {schema_prefix}"AgentNode"  node
            LEFT JOIN {schema_prefix}"AgentGraph" graph
            ON        node."agentGraphId" = graph.id
            WHERE     node."constantInput"::jsonb->'credentials'->>'provider' = 'llm'
            ORDER BY  graph."userId";
            """
        )
        logger.info(f"Fixing LLM credential inputs on {len(broken_nodes)} nodes")
    except Exception as e:
        logger.error(f"Error fixing LLM credential inputs: {e}")

    user_id: str = ""
    user_integrations = None
    for node in broken_nodes:
        if node["user_id"] != user_id:
            # Save queries by only fetching once per user
            user_id = node["user_id"]
            user_integrations = await get_user_integrations(user_id)
        elif not user_integrations:
            raise RuntimeError(f"Impossible state while processing node {node}")

        node_id: str = node["node_id"]
        node_preset_input: dict = node["node_preset_input"]
        credentials_meta: dict = node_preset_input["credentials"]

        credentials = next(
            (
                c
                for c in user_integrations.credentials
                if c.id == credentials_meta["id"]
            ),
            None,
        )
        if not credentials:
            continue
        if credentials.type != "api_key":
            logger.warning(
                f"User {user_id} credentials {credentials.id} with provider 'llm' "
                f"has invalid type '{credentials.type}'"
            )
            continue

        api_key = credentials.api_key.get_secret_value()
        if api_key.startswith("sk-ant-api03-"):
            credentials.provider = credentials_meta["provider"] = "anthropic"
        elif api_key.startswith("sk-"):
            credentials.provider = credentials_meta["provider"] = "openai"
        elif api_key.startswith("gsk_"):
            credentials.provider = credentials_meta["provider"] = "groq"
        else:
            logger.warning(
                f"Could not identify provider from key prefix {api_key[:13]}*****"
            )
            continue

        await store.update_creds(user_id, credentials)
        await AgentNode.prisma().update(
            where={"id": node_id},
            data={"constantInput": SafeJson(node_preset_input)},
        )


async def migrate_llm_models(migrate_to: LlmModel):
    """
    Update all LLM models in all AI blocks that don't exist in the enum.
    Note: Only updates top level LlmModel SchemaFields of blocks (won't update nested fields).
    """
    logger.info("Migrating LLM models")
    # Scan all blocks and search for LlmModel fields
    llm_model_fields: dict[str, str] = {}  # {block_id: field_name}

    # Search for all LlmModel fields
    for block_type in get_blocks().values():
        block = block_type()
        from pydantic.fields import FieldInfo

        fields: dict[str, FieldInfo] = block.input_schema.model_fields

        # Collect top-level LlmModel fields
        for field_name, field in fields.items():
            if field.annotation == LlmModel:
                llm_model_fields[block.id] = field_name

    # Convert enum values to a list of strings for the SQL query
    enum_values = [v.value for v in LlmModel]
    escaped_enum_values = repr(tuple(enum_values))  # hack but works

    # Update each block
    for id, path in llm_model_fields.items():
        query = f"""
            UPDATE platform."AgentNode"
            SET "constantInput" = jsonb_set("constantInput", $1, to_jsonb($2), true)
            WHERE "agentBlockId" = $3
            AND "constantInput" ? ($4)::text
            AND "constantInput"->>($4)::text NOT IN {escaped_enum_values}
            """

        await db.execute_raw(
            query,  # type: ignore - is supposed to be LiteralString
            [path],
            migrate_to.value,
            id,
            path,
        )
