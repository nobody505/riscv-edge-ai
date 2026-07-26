#ifndef _JDK_HWVO_H_
#define _JDK_HWVO_H_

#include <signal.h>
#include <stdio.h>

#include <chrono>
#include <functional>
#include <memory>
#include <string>
#include <thread>

#include "JdkFrame.hpp"
#include "vo.h"

class EXPORT_VISIBILITY JdkVo {
public:
	JdkVo(int width, int height, MppPixelFormat Format = PIXEL_FORMAT_NV12);
	~JdkVo();

	int sendFrame(std::shared_ptr<JdkFrame> frame);

private:
	int width_;
	int height_;
	// MppCodingType payload_;
	MppPixelFormat format_;
	int			   channel_id_;
	MppVoCtx	  *pVoCtx = nullptr;
};

#endif
