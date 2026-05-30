// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

/// @title BlockScamValidationRegistry
/// @notice ERC-8004-compatible validation registry for BlockScam moderation proofs.
/// @dev This is a lightweight demo registry. It stores validation requests and optional validator responses.
contract BlockScamValidationRegistry {
    struct ValidationData {
        address requester;
        address validator;
        uint256 agentId;
        string requestURI;
        bytes32 requestHash;
        uint64 createdAt;
        bool exists;

        bool responded;
        uint8 response; // 0-100
        string responseURI;
        bytes32 responseHash;
        string tag;
        uint64 respondedAt;
    }

    mapping(bytes32 => ValidationData) private validations;
    mapping(uint256 => bytes32[]) private validationsByAgent;
    mapping(address => bytes32[]) private requestsByValidator;

    event ValidationRequested(
        address indexed requester,
        address indexed validator,
        uint256 indexed agentId,
        string requestURI,
        bytes32 requestHash
    );

    event ValidationResponded(
        address indexed validator,
        uint256 indexed agentId,
        bytes32 indexed requestHash,
        uint8 response,
        string responseURI,
        bytes32 responseHash,
        string tag
    );

    function validationRequest(
        address validatorAddress,
        uint256 agentId,
        string calldata requestURI,
        bytes32 requestHash
    ) external {
        require(validatorAddress != address(0), "validator is zero");
        require(agentId > 0, "agentId is zero");
        require(requestHash != bytes32(0), "requestHash is zero");
        require(!validations[requestHash].exists, "request exists");

        validations[requestHash] = ValidationData({
            requester: msg.sender,
            validator: validatorAddress,
            agentId: agentId,
            requestURI: requestURI,
            requestHash: requestHash,
            createdAt: uint64(block.timestamp),
            exists: true,
            responded: false,
            response: 0,
            responseURI: "",
            responseHash: bytes32(0),
            tag: "",
            respondedAt: 0
        });

        validationsByAgent[agentId].push(requestHash);
        requestsByValidator[validatorAddress].push(requestHash);

        emit ValidationRequested(
            msg.sender,
            validatorAddress,
            agentId,
            requestURI,
            requestHash
        );
    }

    function validationResponse(
        bytes32 requestHash,
        uint8 response,
        string calldata responseURI,
        bytes32 responseHash,
        string calldata tag
    ) external {
        ValidationData storage data = validations[requestHash];

        require(data.exists, "request not found");
        require(msg.sender == data.validator, "only validator");
        require(!data.responded, "already responded");
        require(response <= 100, "response > 100");

        data.responded = true;
        data.response = response;
        data.responseURI = responseURI;
        data.responseHash = responseHash;
        data.tag = tag;
        data.respondedAt = uint64(block.timestamp);

        emit ValidationResponded(
            msg.sender,
            data.agentId,
            requestHash,
            response,
            responseURI,
            responseHash,
            tag
        );
    }

    function getValidationStatus(bytes32 requestHash)
        external
        view
        returns (
            bool exists,
            bool responded,
            uint8 response,
            address requester,
            address validator,
            uint256 agentId,
            string memory requestURI,
            string memory responseURI,
            bytes32 responseHash,
            string memory tag
        )
    {
        ValidationData storage data = validations[requestHash];

        return (
            data.exists,
            data.responded,
            data.response,
            data.requester,
            data.validator,
            data.agentId,
            data.requestURI,
            data.responseURI,
            data.responseHash,
            data.tag
        );
    }

    function getAgentValidations(uint256 agentId)
        external
        view
        returns (bytes32[] memory)
    {
        return validationsByAgent[agentId];
    }

    function getValidatorRequests(address validator)
        external
        view
        returns (bytes32[] memory)
    {
        return requestsByValidator[validator];
    }

    function getSummary(uint256 agentId)
        external
        view
        returns (
            uint256 total,
            uint256 completed,
            uint256 averageResponse
        )
    {
        bytes32[] storage list = validationsByAgent[agentId];

        total = list.length;

        if (total == 0) {
            return (0, 0, 0);
        }

        uint256 sum = 0;

        for (uint256 i = 0; i < list.length; i++) {
            ValidationData storage data = validations[list[i]];

            if (data.responded) {
                completed++;
                sum += data.response;
            }
        }

        averageResponse = completed == 0 ? 0 : sum / completed;
    }
}
