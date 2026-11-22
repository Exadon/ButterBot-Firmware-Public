#ifdef CLOCKSTAR_FIRMWARE_BLE_SERVER_H


class Service {
public:
	std::shared_ptr<BLE::Server::Char> addChar(esp_bt_uuid_t uuid, esp_gatt_char_prop_t props = 0);

	// Service-scoped disconnect callback. Fires only when the peer that claimed this service
	// (via a CCCD enable on one of its chars) disconnects — apps that own a service can
	// subscribe here instead of Server::addOnDisconnectCb to avoid spurious teardown when
	// the *other* peer (on a different service) drops.
	using DisconnectCB = std::function<void(const esp_bd_addr_t)>;
	using SubHandle = uint32_t;
	SubHandle addOnDisconnectCb(DisconnectCB cb);
	void removeOnDisconnectCb(SubHandle handle);

	void setPeerNeverPairs();

private:
	friend BLE::Server;
	friend BLE::Server::Char;
	friend BLE::Server::CharInfo;
	Service(esp_gatt_srvc_id_t id);

	BLE::Server* server = nullptr;
	bool peerNeverPairs = false;

	esp_gatt_srvc_id_t id;
	std::vector<std::shared_ptr<BLE::Server::Char>> chars;

	uint16_t hndl = 0xffff;
	void establish(uint16_t hndl);
	std::shared_ptr<Server::Char> charCreated(esp_gatt_status_t status, esp_bt_uuid_t uid, std::unique_ptr<BLE::Server::CharInfo> charInfo);

	// The peers subscribed to this service (a CCCD enable on one of its chars): the controller and the watch
	// remote can both be. Notifications go to all of them; the service's disconnect callbacks fire only when
	// the last one leaves, so one remote dropping doesn't tear down the session the other is still using.
	static constexpr size_t MaxSubscribers = 3;
	std::array<std::array<uint8_t, 6>, MaxSubscribers> subscribers = {};
	size_t subscriberCount = 0;
	// Writes (onPeerSubscribed / onPeerUnsubscribed / onDisconnect) happen on the
	// BLE/BTC task; reads (CharInfo::sendNotif) come from app threads.
	mutable std::mutex peerMut;
	int findSubscriber(const esp_bd_addr_t peer) const; // caller holds peerMut
	void onPeerSubscribed(const esp_bd_addr_t peer);
	// Called from Char::onWrite when a CCCD disable arrives: that peer stops getting notifications.
	void onPeerUnsubscribed(const esp_bd_addr_t peer);
	void onDisconnect(const esp_bd_addr_t peer);

	// Snapshot used by CharInfo::sendNotif: copies the subscribed peers into `out`, returns how many.
	size_t getSubscribedPeers(std::array<std::array<uint8_t, 6>, MaxSubscribers>& out) const;
	bool hasSubscribers() const;

	// Callbacks are added/removed from app threads (Com / Android construction & destruction)
	// and iterated on the BTC task in onDisconnect. Guard with a mutex; iterate over a
	// snapshot taken under the lock so user callbacks don't run with the lock held.
	std::unordered_map<SubHandle, DisconnectCB> onDisconnectCBs;
	SubHandle nextSubHandle = 1;
	mutable std::mutex cbMut;

};


#endif //CLOCKSTAR_FIRMWARE_BLE_SERVER_H
