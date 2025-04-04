# SPDX-FileCopyrightText: 2022-present deepset GmbH <info@deepset.ai>
#
# SPDX-License-Identifier: Apache-2.0

import os
from typing import Any, Dict, List, Optional, Tuple

import httpx
from more_itertools import batched
from openai import APIError
from openai.lib.azure import AzureADTokenProvider, AzureOpenAI
from tqdm import tqdm

from haystack import Document, component, default_from_dict, default_to_dict, logging
from haystack.utils import Secret, deserialize_callable, deserialize_secrets_inplace, serialize_callable

logger = logging.getLogger(__name__)


@component
class AzureOpenAIDocumentEmbedder:
    """
    Calculates document embeddings using OpenAI models deployed on Azure.

    ### Usage example

    ```python
    from haystack import Document
    from haystack.components.embedders import AzureOpenAIDocumentEmbedder

    doc = Document(content="I love pizza!")

    document_embedder = AzureOpenAIDocumentEmbedder()

    result = document_embedder.run([doc])
    print(result['documents'][0].embedding)

    # [0.017020374536514282, -0.023255806416273117, ...]
    ```
    """

    def __init__(  # noqa: PLR0913 (too-many-arguments) # pylint: disable=too-many-positional-arguments
        self,
        azure_endpoint: Optional[str] = None,
        api_version: Optional[str] = "2023-05-15",
        azure_deployment: str = "text-embedding-ada-002",
        dimensions: Optional[int] = None,
        api_key: Optional[Secret] = Secret.from_env_var("AZURE_OPENAI_API_KEY", strict=False),
        azure_ad_token: Optional[Secret] = Secret.from_env_var("AZURE_OPENAI_AD_TOKEN", strict=False),
        organization: Optional[str] = None,
        prefix: str = "",
        suffix: str = "",
        batch_size: int = 32,
        progress_bar: bool = True,
        meta_fields_to_embed: Optional[List[str]] = None,
        embedding_separator: str = "\n",
        timeout: Optional[float] = None,
        max_retries: Optional[int] = None,
        *,
        default_headers: Optional[Dict[str, str]] = None,
        azure_ad_token_provider: Optional[AzureADTokenProvider] = None,
        http_client_kwargs: Optional[Dict[str, Any]] = None,
    ):
        """
        Creates an AzureOpenAIDocumentEmbedder component.

        :param azure_endpoint:
            The endpoint of the model deployed on Azure.
        :param api_version:
            The version of the API to use.
        :param azure_deployment:
            The name of the model deployed on Azure. The default model is text-embedding-ada-002.
        :param dimensions:
            The number of dimensions of the resulting embeddings. Only supported in text-embedding-3
            and later models.
        :param api_key:
            The Azure OpenAI API key.
            You can set it with an environment variable `AZURE_OPENAI_API_KEY`, or pass with this
            parameter during initialization.
        :param azure_ad_token:
            Microsoft Entra ID token, see Microsoft's
            [Entra ID](https://www.microsoft.com/en-us/security/business/identity-access/microsoft-entra-id)
            documentation for more information. You can set it with an environment variable
            `AZURE_OPENAI_AD_TOKEN`, or pass with this parameter during initialization.
            Previously called Azure Active Directory.
        :param organization:
            Your organization ID. See OpenAI's
            [Setting Up Your Organization](https://platform.openai.com/docs/guides/production-best-practices/setting-up-your-organization)
            for more information.
        :param prefix:
            A string to add at the beginning of each text.
        :param suffix:
            A string to add at the end of each text.
        :param batch_size:
            Number of documents to embed at once.
        :param progress_bar:
            If `True`, shows a progress bar when running.
        :param meta_fields_to_embed:
            List of metadata fields to embed along with the document text.
        :param embedding_separator:
            Separator used to concatenate the metadata fields to the document text.
        :param timeout: The timeout for `AzureOpenAI` client calls, in seconds.
            If not set, defaults to either the
            `OPENAI_TIMEOUT` environment variable, or 30 seconds.
        :param max_retries: Maximum number of retries to contact AzureOpenAI after an internal error.
            If not set, defaults to either the `OPENAI_MAX_RETRIES` environment variable or to 5 retries.
        :param default_headers: Default headers to send to the AzureOpenAI client.
        :param azure_ad_token_provider: A function that returns an Azure Active Directory token, will be invoked on
            every request.
        :param http_client_kwargs: A dictionary of keyword arguments to configure a custom httpx.Client.
        """
        # if not provided as a parameter, azure_endpoint is read from the env var AZURE_OPENAI_ENDPOINT
        azure_endpoint = azure_endpoint or os.environ.get("AZURE_OPENAI_ENDPOINT")
        if not azure_endpoint:
            raise ValueError("Please provide an Azure endpoint or set the environment variable AZURE_OPENAI_ENDPOINT.")

        if api_key is None and azure_ad_token is None:
            raise ValueError("Please provide an API key or an Azure Active Directory token.")

        self.api_key = api_key
        self.azure_ad_token = azure_ad_token
        self.api_version = api_version
        self.azure_endpoint = azure_endpoint
        self.azure_deployment = azure_deployment
        self.dimensions = dimensions
        self.organization = organization
        self.prefix = prefix
        self.suffix = suffix
        self.batch_size = batch_size
        self.progress_bar = progress_bar
        self.meta_fields_to_embed = meta_fields_to_embed or []
        self.embedding_separator = embedding_separator
        self.timeout = timeout if timeout is not None else float(os.environ.get("OPENAI_TIMEOUT", "30.0"))
        self.max_retries = max_retries if max_retries is not None else int(os.environ.get("OPENAI_MAX_RETRIES", "5"))
        self.default_headers = default_headers or {}
        self.azure_ad_token_provider = azure_ad_token_provider
        self.http_client_kwargs = http_client_kwargs

        self._client = AzureOpenAI(
            api_version=api_version,
            azure_endpoint=azure_endpoint,
            azure_deployment=azure_deployment,
            azure_ad_token_provider=azure_ad_token_provider,
            api_key=api_key.resolve_value() if api_key is not None else None,
            azure_ad_token=azure_ad_token.resolve_value() if azure_ad_token is not None else None,
            organization=organization,
            timeout=self.timeout,
            max_retries=self.max_retries,
            default_headers=self.default_headers,
            http_client=self._init_http_client(),
        )

    def _init_http_client(self):
        """Internal method to initialize the httpx.Client."""
        if self.http_client_kwargs:
            if not isinstance(self.http_client_kwargs, dict):
                raise TypeError("The parameter 'http_client_kwargs' must be a dictionary.")
            return httpx.Client(**self.http_client_kwargs)
        return None

    def _get_telemetry_data(self) -> Dict[str, Any]:
        """
        Data that is sent to Posthog for usage analytics.
        """
        return {"model": self.azure_deployment}

    def to_dict(self) -> Dict[str, Any]:
        """
        Serializes the component to a dictionary.

        :returns:
            Dictionary with serialized data.
        """
        azure_ad_token_provider_name = None
        if self.azure_ad_token_provider:
            azure_ad_token_provider_name = serialize_callable(self.azure_ad_token_provider)
        return default_to_dict(
            self,
            azure_endpoint=self.azure_endpoint,
            azure_deployment=self.azure_deployment,
            dimensions=self.dimensions,
            organization=self.organization,
            api_version=self.api_version,
            prefix=self.prefix,
            suffix=self.suffix,
            batch_size=self.batch_size,
            progress_bar=self.progress_bar,
            meta_fields_to_embed=self.meta_fields_to_embed,
            embedding_separator=self.embedding_separator,
            api_key=self.api_key.to_dict() if self.api_key is not None else None,
            azure_ad_token=self.azure_ad_token.to_dict() if self.azure_ad_token is not None else None,
            timeout=self.timeout,
            max_retries=self.max_retries,
            default_headers=self.default_headers,
            azure_ad_token_provider=azure_ad_token_provider_name,
            http_client_kwargs=self.http_client_kwargs,
        )

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "AzureOpenAIDocumentEmbedder":
        """
        Deserializes the component from a dictionary.

        :param data:
            Dictionary to deserialize from.
        :returns:
            Deserialized component.
        """
        deserialize_secrets_inplace(data["init_parameters"], keys=["api_key", "azure_ad_token"])
        serialized_azure_ad_token_provider = data["init_parameters"].get("azure_ad_token_provider")
        if serialized_azure_ad_token_provider:
            data["init_parameters"]["azure_ad_token_provider"] = deserialize_callable(
                serialized_azure_ad_token_provider
            )
        return default_from_dict(cls, data)

    def _prepare_texts_to_embed(self, documents: List[Document]) -> Dict[str, str]:
        """
        Prepare the texts to embed by concatenating the Document text with the metadata fields to embed.
        """
        texts_to_embed = {}
        for doc in documents:
            meta_values_to_embed = [
                str(doc.meta[key]) for key in self.meta_fields_to_embed if key in doc.meta and doc.meta[key] is not None
            ]

            text_to_embed = (
                self.prefix + self.embedding_separator.join(meta_values_to_embed + [doc.content or ""]) + self.suffix
            ).replace("\n", " ")

            texts_to_embed[doc.id] = text_to_embed
        return texts_to_embed

    def _embed_batch(self, texts_to_embed: Dict[str, str], batch_size: int) -> Tuple[List[List[float]], Dict[str, Any]]:
        """
        Embed a list of texts in batches.
        """

        all_embeddings: List[List[float]] = []
        meta: Dict[str, Any] = {"model": "", "usage": {"prompt_tokens": 0, "total_tokens": 0}}

        for batch in tqdm(
            batched(texts_to_embed.items(), batch_size), disable=not self.progress_bar, desc="Calculating embeddings"
        ):
            args: Dict[str, Any] = {"model": self.azure_deployment, "input": [b[1] for b in batch]}

            if self.dimensions is not None:
                args["dimensions"] = self.dimensions

            try:
                response = self._client.embeddings.create(**args)
            except APIError as e:
                # Log the error but continue processing
                ids = ", ".join(b[0] for b in batch)
                logger.exception(f"Failed embedding of documents {ids} caused by {e}")
                continue

            embeddings = [el.embedding for el in response.data]
            all_embeddings.extend(embeddings)

            # Update the meta information only once if it's empty
            if not meta["model"]:
                meta["model"] = response.model
                meta["usage"] = dict(response.usage)
            else:
                # Update the usage tokens
                meta["usage"]["prompt_tokens"] += response.usage.prompt_tokens
                meta["usage"]["total_tokens"] += response.usage.total_tokens

        return all_embeddings, meta

    @component.output_types(documents=List[Document], meta=Dict[str, Any])
    def run(self, documents: List[Document]) -> Dict[str, Any]:
        """
        Embeds a list of documents.

        :param documents:
            Documents to embed.

        :returns:
            A dictionary with the following keys:
            - `documents`: A list of documents with embeddings.
            - `meta`: Information about the usage of the model.
        """
        if not (isinstance(documents, list) and all(isinstance(doc, Document) for doc in documents)):
            raise TypeError("Input must be a list of Document instances. For strings, use AzureOpenAITextEmbedder.")

        texts_to_embed = self._prepare_texts_to_embed(documents=documents)
        embeddings, meta = self._embed_batch(texts_to_embed=texts_to_embed, batch_size=self.batch_size)

        # Assign the corresponding embeddings to each document
        for doc, emb in zip(documents, embeddings):
            doc.embedding = emb

        return {"documents": documents, "meta": meta}
