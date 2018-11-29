# 
# Copyright 2018 Kionix Inc.
#
import struct
from kx_lib.kx_util import DelayedKeyboardInterrupt
from kx_lib.kx_sensor_base import AxisMapper
from kx_lib.kx_exception import ProtocolBus1Exception, EvaluationKitException
from kx_lib import kx_logger
from kx_lib.kx_configuration_enum import BUS1_I2C, BUS1_SPI, BUS1_ADC, BUS1_GPIO, \
    CFG_SAD, CFG_CS, CFG_TARGET, CFG_ADC_RESOLUTION, CFG_SPI_PROTOCOL, \
    CFG_POLARITY, EVKIT_GPIO_PIN_SENSE_HIGH, EVKIT_GPIO_PIN_SENSE_LOW, \
     CFG_PULLUP, EVKIT_GPIO_PIN_NOPULL, EVKIT_GPIO_PIN_PULLDOWN, EVKIT_GPIO_PIN_PULLUP, CFG_AXIS_MAP
from kx_lib.kx_data_logger import SensorDataLogger
LOGGER = kx_logger.get_logger(__name__)

# LOGGER.setLevel(kx_logger.DEBUG)
# LOGGER.setLevel(kx_logger.INFO)

class DataDim(object): pass # TODO 1
class SensorDataDim(DataDim): pass # TODO 1
class PacketCounterDim(DataDim): pass # TODO 1

class ExtraData(object):
    "Extra data what can be subscribed in stream request messages"
    reg_packet_count_8 = [0xff, 0x00, 0x00]
    hdr_packet_count_8 = '!ind'
    fmt_packet_count_8 = 'B'


class RequestMessageDefinition(object):
    def __init__(self, sensor, fmt, hdr, reg=None, pin_index=None, timer=None):
        """Container for data needed for creating macro request message and parsing indication messages

        Args:
            sensor(SensorDriver)
            fmt(string): formatter for struct.unpack how to interpret binary data
            hdr(string): Header row for log, dimensions separated with ! mark.
            reg(int): register number where reading starts.
            pin_index(int): Sensor's interrupt line number.
            timer(float): delay in seconds when using timer based data stream

        Allowed parameter combinations:
            reg+pin_index: read register when interrupt triggers
            reg+timer: read register when timer triggers
            timer+gpio_pin: read ADC when timer triggers

        """
        # TODO 3 is it needed to have "read ADC when interrupt triggers"?
        if pin_index is not None:
            self.gpio_pin = sensor.connection_manager.get_physical_pin_for_sensor(sensor, pin_index)
            if isinstance(pin_index, int):
                pin_index = [pin_index]  # change to list for value checking
                for _pin in pin_index:
                    if _pin not in sensor.int_pins:
                        raise EvaluationKitException(
                            'Request logical interrupt pin {} not supported in selected sensor. Supported pins are {}'.format(
                                pin_index, sensor.int_pins))

            if timer is not None and reg is not None:
                raise EvaluationKitException('timer cannot ber used together with gpio interrupt')

        self.msg_fmt = fmt
        self.msg_hdr = hdr
        self.sensor = sensor
        self.reg = reg
        self.msg_size = struct.calcsize(self.msg_fmt)
        self.msg_req = []  # protocol v1 messages generated by this request
        self.timer = timer
        self.axis_mapper = AxisMapper(channel_header=hdr, axis_map=sensor.resource[CFG_AXIS_MAP])

    def __str__(self):
        return 'RequestMessageDefinition %s' % self.msg_hdr


class StreamConfig(object):
    def __init__(self, sensor=None):
        # FIXME: Add support for protocol 1 when using stream_config_utils.py for generating streams

        # if sensor not defined here then must be defined on define_request_message()
        # TODO 2 does not work if sensor not defined here, make test for this use case
        self.sensor = sensor
        self.macro_id_list = []  # list of all data streams requested. Integer list.
        self.msg_ind_dict = {}  # {macro_id_list:RequestMessageDefinition}
        self.adapter = self.sensor.connection_manager.kx_adapter

        if self.adapter.stream_support is False:
            raise EvaluationKitException("Adapter %s does not support data streaming." % self.adapter)

        # map methods based on protocol version
        if self.adapter.engine.version == 2:
            self._start_streaming = self._start_streaming_v2
            self._stop_streaming = self._stop_streaming_v2
            self.define_request_message = self._define_request_message_v2

        elif self.adapter.engine.version == 1:
            self._start_streaming = self._start_streaming_v1
            self._stop_streaming = self._stop_streaming_v1
            self.define_request_message = self._define_request_message_v1

        else:
            raise EvaluationKitException('Unsupported protocol engine version.')

        protocol = self.adapter.protocol # kx_protocol or kx_protocol_2 module

        # TODO 2 move to protocol
        self.sense_dict = {
            EVKIT_GPIO_PIN_SENSE_HIGH : protocol.EVKIT_GPIO_PIN_SENSE_HIGH,
            EVKIT_GPIO_PIN_SENSE_LOW : protocol.EVKIT_GPIO_PIN_SENSE_LOW
        }
        # TODO 2 move to protocol
        self.pullup_dict = {
            EVKIT_GPIO_PIN_NOPULL : protocol.EVKIT_GPIO_PIN_NOPULL,
            EVKIT_GPIO_PIN_PULLDOWN : protocol.EVKIT_GPIO_PIN_PULLDOWN,
            EVKIT_GPIO_PIN_PULLUP : protocol.EVKIT_GPIO_PIN_PULLUP
        }

#    def add_channel(self,[dim_list]):
#        pass # TODO 1

    def _define_request_message_v2(self, sensor=None, fmt=None, hdr=None, reg=None, pin_index=None, timer=None):
        """Construct stream request message

        Args:
            sensor_driver(SensorDriver): sensor driver instance in case of multiple sensors in use. Defaults to None.
                NOTE : In one sensor case the sensor driver instance is given in constructor.
            fmt(string): format definition how received binary data is interpreted as struct module format string.
            hdr(string): sensor data log file header, channel names separated with ! mark.
                Example :ch!ax!ay!az
            reg(int): register number from reading starts. NOTE: fmt defines how many bytes are read.
            pin_index(int): sensor's logical interrupt pin which triggers register reading.
            pin_index(list): list of sensor's logical gpio pins from read ADC values
            timer(float): interval value if using timer based polling. When timer is set then pin_index must be unset and vice versa.

        """
        LOGGER.debug('>')

        assert not None in [fmt, hdr], 'Mandatory values not defined'

        if sensor is None:
            sensor = self.sensor

        if timer is None:
            assert pin_index in sensor.int_pins, 'Sensor does not have interrput pin %d.' % pin_index

        LOGGER.debug('Stream request for {}'.format(self.sensor.name))
        message = RequestMessageDefinition(sensor, fmt, hdr, reg, pin_index, timer)

        # (u'spi', {u'cs': 0, u'gpio1': 0, u'target': 1, u'name': u'KX122'})
        # (u'i2c', {u'gpio1': 0, u'SAD': 31, u'target': 4, u'name': u'KX122'})
        bus1_name = sensor.selected_connectivity

        protocol = self.adapter.protocol

        #
        # Create macro request
        #

        if message.reg is None:
            # FOR ADC reading?
            LOGGER.debug('EVKIT_MACRO_TYPE_POLL {}'.format(message.gpio_pin))
            # TODO 2 determine timer scale dynamically
            req = protocol.create_macro_req(
                trigger_type=protocol.EVKIT_MACRO_TYPE_POLL,
                timer_scale=protocol.EVKIT_TIME_SCALE_MS,
                timer_value=int(message.timer * 1000)
                )

        elif message.timer is not None:
            # FOR timer polling digital bus
            # TODO 2 combine these two?
            LOGGER.debug('EVKIT_MACRO_TYPE_POLL {}seconds'.format(message.timer))
            # TODO 2 determine timer scale dynamically
            req = protocol.create_macro_req(
                trigger_type=protocol.EVKIT_MACRO_TYPE_POLL,
                timer_scale=protocol.EVKIT_TIME_SCALE_MS,
                timer_value=int(message.timer * 1000)
                )

        elif message.gpio_pin is not None:

            LOGGER.debug('EVKIT_MACRO_TYPE_INTR %s' % message.gpio_pin)
            req = protocol.create_macro_req(
                trigger_type=protocol.EVKIT_MACRO_TYPE_INTR,
                gpio_pin=message.gpio_pin,
                gpio_sense=self.sense_dict[sensor.resource[CFG_POLARITY]],
                gpio_pullup=self.pullup_dict[sensor.resource[CFG_PULLUP]]
                )
            # TODO 2 update _pin_mode_cache.

        else:
            raise EvaluationKitException('No rule to make request.')

        #
        # send macro request and store macro
        #

        self.adapter.send_message(req)
        _, macro_id = self.adapter.receive_message(waif_for_message=protocol.EVKIT_MSG_CREATE_MACRO_RESP) #  message_type, macro_id
        self.macro_id_list.append(macro_id)
        self.msg_ind_dict[macro_id] = message
        message.msg_req.append(req)
        LOGGER.debug('Macro created with id %d', macro_id)

        #
        # Defene what action is done when macro triggers
        #

        if bus1_name == BUS1_I2C:
            LOGGER.debug("EVKIT_MACRO_ACTION_READ over i2c")

            req = protocol.add_macro_action_req(
                macro_id,
                action=protocol.EVKIT_MACRO_ACTION_READ,
                target=sensor.resource[CFG_TARGET],
                identifier=sensor.resource[CFG_SAD],
                append=True,
                start_register=message.reg,
                bytes_to_read=message.msg_size - 1)

            LOGGER.debug(req)
            self.adapter.send_message(req)
            self.adapter.receive_message(waif_for_message=protocol.EVKIT_MSG_ADD_MACRO_ACTION_RESP)
            message.msg_req.append(req)

        elif bus1_name == BUS1_SPI:
            LOGGER.debug("EVKIT_MACRO_ACTION_READ over spi")
            assert sensor.resource[CFG_SPI_PROTOCOL] in [0, 1]

            if sensor.resource[CFG_SPI_PROTOCOL] == 1:
                # With Kionix components, MSB must be set 1 to indicate reading
                message.reg = message.reg | 1 << 7

            req = protocol.add_macro_action_req(
                macro_id,
                action=protocol.EVKIT_MACRO_ACTION_READ,
                target=sensor.resource[CFG_TARGET],
                identifier=sensor.resource[CFG_CS],
                append=True,
                start_register=message.reg,
                bytes_to_read=message.msg_size - 1)

            LOGGER.debug(req)
            self.adapter.send_message(req)
            message.msg_req.append(req)
            self.adapter.receive_message(waif_for_message=protocol.EVKIT_MSG_ADD_MACRO_ACTION_RESP)

        elif bus1_name == BUS1_ADC:
            LOGGER.debug("EVKIT_MACRO_ACTION_ADC_READ")

            if isinstance(message.gpio_pin, int):
                message.gpio_pin = [message.gpio_pin]

            self.sensor.connection_manager.gpio_config_for_adc(self.sensor)

            for pin in message.gpio_pin:

                req2 = protocol.add_macro_action_req(
                    macro_id=macro_id,
                    action=protocol.EVKIT_MACRO_ACTION_ADC_READ,
                    target=sensor.resource[CFG_TARGET],
                    identifier=pin,
                    append=True,
                    adc_oversample=sensor.resource['adc_msg_oversampling_enum'],
                    adc_gain=sensor.resource['adc_msg_gain_enum'],
                    adc_resolution=sensor.resource[CFG_ADC_RESOLUTION],
                    adc_acq_time_us=sensor.resource['adc_msg_conversion_time_us'])

                LOGGER.debug(req2)
                self.adapter.send_message(req2)
                message.msg_req.append(req2)
                self.adapter.receive_message(waif_for_message=protocol.EVKIT_MSG_ADD_MACRO_ACTION_RESP)

        elif bus1_name == BUS1_GPIO:
            # TODO 3 BUS1_GPIO data stream
            raise EvaluationKitException('Unsupported bus1 {}'.format(bus1_name))
        else:
            raise EvaluationKitException('Unsupported bus1 {}'.format(bus1_name))


        LOGGER.debug('<')

    def _define_request_message_v1(self, sensor=None, fmt=None, hdr=None, reg=None, pin_index=None, timer=None):

        assert not None in [fmt, hdr, reg], 'All values must be defined'

        if timer is not None:
            raise EvaluationKitException('Timer not supported in this firmware version')

        # NOTE : =None needed due API change and to prevent API break
        if sensor is not None:
            assert pin_index in sensor.int_pins, 'Sensor does not have interrput pin %d.' % pin_index
            message = RequestMessageDefinition(sensor, fmt, hdr, reg, pin_index)
        else:
            assert pin_index in self.sensor.int_pins, 'Sensor does not have interrput pin %d.' % pin_index
            message = RequestMessageDefinition(self.sensor, fmt, hdr, reg, pin_index)
        self.macro_id_list.append(message)
        self._prepare_request_message_v1(message)

    def _prepare_request_message_v1(self, message):
        ##message.gpio_pin = message.sensor._bus._gpio_pin_index[message.pin_index]

        # uses self.msg_size-1 because channel number will be added to response
        # (it is not included in payload)
        message.msg_size = struct.calcsize(message.msg_fmt)

        assert isinstance(message.reg, (int, list)), 'Register variable type must be int or list.'

        # simple way to define what to read
        if isinstance(message.reg, int):
            message.msg_req = [message.gpio_pin,
                               [message.sensor.i2c_address(),
                                message.reg,
                                message.msg_size - 1]]

        else:
            # Advanced way. "Manual" definition of request payload
            message.msg_req = [message.gpio_pin, message.reg]

    def _start_streaming_v1(self):
        # send stream start requests to FW
        for request in self.macro_id_list:
            LOGGER.debug(">Enable interrupt request %s %s" % (request.sensor.resource[CFG_POLARITY], request.sensor.resource[CFG_PULLUP]))
            pin, payload = request.msg_req
            req = self.adapter.protocol.interrupt_enable_req(
                pin=pin,
                payload_definition=payload,
                sense=self.sense_dict[request.sensor.resource[CFG_POLARITY]],
                pull=self.pullup_dict[request.sensor.resource[CFG_PULLUP]])

            # TODO 2 update _pin_mode_cache.

            LOGGER.debug(req)
            self.adapter.send_message(req)
            status, index = self.adapter.receive_message(self.adapter.protocol.EVKIT_MSG_ENABLE_INT_RESP)
            del status  # unused
            self.msg_ind_dict[index] = request
        LOGGER.debug("<Enable interrupt request")

    def _stop_streaming_v1(self):
        LOGGER.debug(">Disable interrupt request")

        # flushing bus here could help if high speed data stream causes overflow
        # NOTE this can scrap stream data especially if having multiple streams running
        # it is considered to be OK when stopgging all data streams when application ends
        # self.adapter.bus2.flush()

        # send stream stop requests to FW in reversed order
        for request in reversed(self.macro_id_list):
            req = self.adapter.protocol.interrupt_disable_req(request.gpio_pin)
            self.adapter.send_message(req)
            self.adapter.receive_message(self.adapter.protocol.EVKIT_MSG_DISABLE_INT_RESP)

        # buffered data could be flushed also here
        # self.adapter.bus2.flush()

        LOGGER.debug("<Disable interrupt request")

    def _start_streaming_v2(self):
        LOGGER.debug(">_start_streaming")
        for macro_id in self.macro_id_list:
            req = self.adapter.protocol.start_macro_action_req(macro_id)
            LOGGER.debug(req)
            self.adapter.send_message(req)
            self.adapter.receive_message(waif_for_message=self.adapter.protocol.EVKIT_MSG_START_MACRO_RESP)
            self.msg_ind_dict[macro_id].msg_req.append(req)
        LOGGER.debug("<_start_streaming")

    def _stop_streaming_v2(self):
        LOGGER.debug(">stop streaming")
        for macro_id in reversed(self.macro_id_list):
            req = self.adapter.protocol.remove_macro_req(macro_id)
            LOGGER.debug(req)

            # flushing bus here could help if high speed data stream causes overflow
            # NOTE this can scrap stream data especially if having multiple streams running
            # it is considered to be OK when stopgging all data streams when application ends
            # self.adapter.bus2.flush()
            self.adapter.send_message(req)

            # NOTE using dont_cache=True could do same flush
            result = self.adapter.receive_message(waif_for_message=self.adapter.protocol.EVKIT_MSG_REMOVE_MACRO_RESP)

            self.msg_ind_dict[macro_id].msg_req.append(req)
            LOGGER.debug(result)

            # buffered data could be flushed also here
            # self.adapter.bus2.flush()

        LOGGER.debug("<stop streaming")

    def read_data_stream(self,
                         loop=None,
                         console=True,
                         log_file_name=None,
                         callback=None,
                         max_timeout_count=0,
                         additional_info=None):
        """Main loop for reading stream data after data streams are activated.

        Args:
            loop(int/None): Number of values to read. If None then infinite loop until KeyboardInterrupt is received (defaults to None)
            concole(bool): print values to console (defaults to True)
            log_file_name(string/None): Log file to write. (defaults to None)
            callback(function): function to call after new data is received.
            max_timeout_count(int/None): break after bus2 time out count is reached. If none then loop forever regardless timeouts
            additional_info(string): info passed to SensorDataLogger instance

        """
        count = 0  # count of received data samples
        timeout_count = 0  # how many timeouts received

        data_logger = SensorDataLogger(console=console,
                                       log_file_name=log_file_name,
                                       additional_info=additional_info)

        # subscribe sensor data from FW
        self._start_streaming()

        # On FW1 channels are known only after that _start_streaming()
        # print out header text, replace text "ch" with channel number
        for channel, request in iter(self.msg_ind_dict.items()):
            data_logger.add_channel(request.msg_hdr, channel)

        data_logger.start()

        try:
            # main loop for reading the data
            while (loop is None) or (count < loop):
                with DelayedKeyboardInterrupt():
                    # TODO 3 error handling if message was not macro message : macro_index >= EVKIT_MSG_MACRO_IND_BASE etc. logic
                    macro_index, resp = self.adapter.receive_message()

                if resp is None:
                    LOGGER.debug("Timeout when receiving data")
                    timeout_count += 1

                    if max_timeout_count is not None \
                       and timeout_count >= max_timeout_count:

                        raise ProtocolBus1Exception('Timeout when receiving data. Max timeout count reached.')

                    continue

                # find correct message type to get information how message is interpreted
                received_messsage_type = self.msg_ind_dict[macro_index]

                if len(resp) != received_messsage_type.msg_size:
                    LOGGER.error("Length of received message was wrong (%d). Expected (%d)" % (len(resp), received_messsage_type.msg_size))
                else:
                    fmt = self.msg_ind_dict[macro_index].msg_fmt
                    # unpack the raw data
                    data = struct.unpack(fmt, resp)
                    # rotate if 3d data
                    data = self.msg_ind_dict[macro_index].axis_mapper.map_xyz_axis(data)
                    # log the data
                    data_logger.feed_values(data)

                    count += 1

                if callback is not None:
                    # callback function returns False if need to stop reading
                    if callback(data) is False:
                        break

        except KeyboardInterrupt:
            # CTRL+C will stop data reading
            pass

        finally:
            # after CTRL+C or other exception, print end time stamp and stop reading sensor data

            # unsibscribe data from FW
            self._stop_streaming()

            data_logger.stop()

            if count == 0:
                LOGGER.error("No stream data received.")
