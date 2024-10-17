
var sendto = Module.findExportByName(null, "sendto");
Interceptor.attach(sendto, {
    onEnter: function (args) {
        var sockfd = args[0];
        var buf = args[1];
        var len = args[2].toInt32();

        var sendData = Memory.readByteArray(buf, len);
        console.log("[UDP] Data sent: " + hexdump(sendData, {
            offset: 0,
            length: len,
            header: true,
            ansi: true
        }));
    }
});

var recvfrom = Module.findExportByName(null, "recvfrom");
Interceptor.attach(recvfrom, {
    onEnter: function (args) {
        this.sockfd = args[0];
        this.buf = args[1];
        this.len = args[2].toInt32();
    },
    onLeave: function (retval) {
        if (retval.toInt32() > 0) {
            var recvData = Memory.readByteArray(this.buf, retval.toInt32());
            console.log("[UDP] Data received: " + hexdump(recvData, {
                offset: 0,
                length: retval.toInt32(),
                header: true,
                ansi: true
            }));
        }
    }
});
